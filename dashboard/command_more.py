"""
UXR-06..09: the rest of the redesigned console, next to the Command Center (/command).

    GET  /command/signals                 Signal Explorer: ranked EOD signals, factor bars, a plain-language
                                          "why", and the gap between the signal price and the live price
    GET  /command/setup                   Trade Setup: steppers for qty / entry / target / stop, live Rs risk,
                                          reward, R:R and charges, the pre-trade checklist, session + save
    GET  /command/monitor                 Live Monitor: positions on a stop <- price -> target bar, the rules
                                          watching (and why a session holds one), the order log with causes
    GET  /command/journal                 Journal: per-session trades / successful / win rate / avg R / P&L,
                                          hit rate by score band, a straight-line Dec 2027 range (not a forecast)

    GET  /api/command/signals?signal=&q=&limit=
    GET  /api/command/preview?symbol=&side=&qty=&entry=&target=&stop=&session=
    POST /api/command/setup               save the setup as an order rule (orders/rules.py create_rule, which
                                          watches -- it never sends by itself) and link it to the session
    GET  /api/command/monitor
    GET  /api/command/journal

Everything here reads, except POST /api/command/setup, which is token-guarded and creates a rule the
existing confirmation gate still sits in front of. /api/command/* is owner-only (enterprise/authz.py).
"""

# No `from __future__ import annotations` (FastAPI must see the real Request class).
import json
import math
from datetime import date, datetime

from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from dashboard.command_routes import _broker, _feed, _finite, _market_open, _rows

FACTORS = [("vpi", "Volatility potential (VPI)"), ("mri", "Momentum (MRI)"), ("rri", "Recovery (RRI)"),
           ("zpi", "Buy zone (ZPI)"), ("acs", "Composite (ACS)"), ("spi", "Stability (SPI)"),
           ("msi", "MSI"), ("tech_score", "Technical"), ("fund_score", "Fundamental"),
           ("inst_score", "Institutional"), ("news_score", "News"), ("cri", "Crash risk (CRI, lower is better)")]
GAP_WARN_PCT = 3.0
END_2027 = date(2027, 12, 31)


def _f(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _last_live(conn, syms) -> dict:
    if not syms:
        return {}
    out = {}
    for i in range(0, len(syms), 400):
        part = syms[i:i + 400]
        marks = ",".join("?" * len(part))
        for r in _rows(conn, f"SELECT q.symbol, q.ltp, q.timestamp FROM live_quotes q JOIN (SELECT symbol, MAX(timestamp) ts "
                             f"FROM live_quotes WHERE symbol IN ({marks}) GROUP BY symbol) m "
                             f"ON m.symbol = q.symbol AND m.ts = q.timestamp", tuple(part)):
            out[r["symbol"]] = (r["ltp"], r["timestamp"])
    return out


# -- UXR-06 Signal Explorer ---------------------------------------------------------------------

def _story(r, sig_date) -> str:
    tops = [t for t in (r.get("top_factor_1"), r.get("top_factor_2"), r.get("top_factor_3")) if t]
    cri = _f(r.get("cri"))
    risk = "unknown" if cri is None else "low" if cri < 40 else "moderate" if cri < 60 else "high"
    s = (f"{r['symbol']} is ranked #{r.get('atip_rank') or '—'} with an ATIP score of "
         f"{round(r['atip_score']) if r.get('atip_score') is not None else '—'} and a {r.get('signal') or 'no'} signal")
    if r.get("confidence") is not None:
        s += f" at {round(r['confidence'])}% confidence"
    s += "."
    if tops:
        s += " It leans most on " + ", ".join(tops) + "."
    s += f" Crash risk reads {risk}" + (f" ({round(cri)})" if cri is not None else "") + "."
    g = r.get("gap_pct")
    if g is not None:
        s += (f" Since the close of {sig_date} the price has moved {g:+.2f}%"
              + (" — past the 3% mark, so re-check before acting." if abs(g) >= GAP_WARN_PCT else "."))
    return s


def signals(conn, signal="ALL", q="", limit=60) -> dict:
    d = _rows(conn, "SELECT MAX(date) AS d FROM ai_scores")
    sig_date = str(d[0]["d"]) if d and d[0]["d"] else None
    if not sig_date:
        return {"date": None, "rows": [], "counts": {}}
    where, params = ["date = ?"], [sig_date]
    signal = str(signal or "ALL").upper()
    if signal != "ALL":
        where.append("`signal` = ?"); params.append(signal)
    if q:
        where.append("symbol LIKE ?"); params.append(f"%{str(q).upper().strip()}%")
    cols = ", ".join(k for k, _ in FACTORS)
    rows = _rows(conn, f"SELECT symbol, `signal`, atip_score, atip_rank, confidence, regime, top_factor_1, "
                       f"top_factor_2, top_factor_3, {cols} FROM ai_scores WHERE {' AND '.join(where)} "
                       f"ORDER BY atip_score DESC LIMIT ?", (*params, max(1, min(int(limit or 60), 500))))
    counts = {r["signal"]: r["n"] for r in
              _rows(conn, "SELECT `signal`, COUNT(*) AS n FROM ai_scores WHERE date = ? GROUP BY `signal`", (sig_date,))}
    syms = [r["symbol"] for r in rows]
    closes = {}
    if syms:
        marks = ",".join("?" * len(syms))
        closes = {r["symbol"]: r["close"] for r in
                  _rows(conn, f"SELECT symbol, close FROM prices_daily WHERE date = ? AND symbol IN ({marks})",
                        (sig_date, *syms))}
    live = _last_live(conn, syms)
    for r in rows:
        sp = closes.get(r["symbol"]); lp, ts = live.get(r["symbol"], (None, None))
        r["signal_price"], r["live_price"], r["live_at"] = sp, lp, ts
        r["gap_pct"] = round((lp - sp) / sp * 100, 2) if sp and lp else None
        r["factors"] = [{"key": k, "label": lab, "value": _f(r.get(k)), "inverse": k == "cri"} for k, lab in FACTORS]
        r["story"] = _story(r, sig_date)
    return {"date": sig_date, "rows": rows, "counts": counts, "gap_warn_pct": GAP_WARN_PCT}


# -- UXR-07 Trade Setup --------------------------------------------------------------------------

def _num(v, name, positive=True):
    x = _f(v)
    if x is None or (positive and x <= 0):
        raise ValueError(f"{name} must be a positive number")
    return x


def preview(conn, symbol, side="BUY", qty=None, entry=None, target=None, stop=None, session=None) -> dict:
    from backtest.costs import cost_model
    from orders import risk, sessions
    symbol = str(symbol or "").upper().strip()
    if not symbol:
        raise ValueError("symbol is required")
    side = str(side or "BUY").upper()
    if side not in ("BUY", "SELL"):
        raise ValueError("side must be BUY or SELL")
    ref, ref_src = risk.reference_price(conn, symbol)
    entry = _f(entry) or ref
    if not entry:
        raise ValueError(f"no price for {symbol}: give an entry price")
    qty = int(_num(qty, "qty")) if qty not in (None, "") else None
    target, stop = _f(target), _f(stop)
    long_ = side == "BUY"
    if stop is not None and ((long_ and stop >= entry) or (not long_ and stop <= entry)):
        raise ValueError(f"the stop must be {'below' if long_ else 'above'} the entry for a {side}")
    if target is not None and ((long_ and target <= entry) or (not long_ and target >= entry)):
        raise ValueError(f"the target must be {'above' if long_ else 'below'} the entry for a {side}")
    sizing = None
    if qty is None:
        if stop is None or not long_:
            raise ValueError("give a quantity (or a stop below a BUY entry to size it from your risk settings)")
        sizing = risk.size_for_account(conn, entry, stop_price=stop)
        qty = int(sizing["quantity"] or 0)
        if qty < 1:
            raise ValueError(f"risk sizing gave no position: {sizing['reason']}")
    value = qty * entry
    cm = cost_model("nse_delivery")
    exit_side = "SELL" if long_ else "BUY"
    c_in = cm.total(side, value)
    risk_rs = abs(entry - stop) * qty if stop is not None else None
    reward_rs = abs(target - entry) * qty if target is not None else None
    c_stop = cm.total(exit_side, stop * qty) if stop is not None else None
    c_tgt = cm.total(exit_side, target * qty) if target is not None else None
    net_risk = risk_rs + c_in + c_stop if risk_rs is not None else None
    net_reward = reward_rs - c_in - c_tgt if reward_rs is not None else None

    env = _broker()["broker_env"]
    lim = risk.limits()
    try:
        from portfolio.pnl import risk_state
        st = risk_state(conn, "LIVE" if env == "LIVE" else "PAPER")
    except Exception:
        st = {}
    cap = lim.get("max_daily_loss_value")
    lost = max(0.0, -(st.get("day_pnl") or 0.0)) if st.get("day_pnl") is not None else None
    budget_left = (cap - lost) if cap and lost is not None else None

    checks = []

    def chk(name, ok, detail, blocking=True):
        checks.append({"check": name, "ok": bool(ok), "detail": detail, "blocking": blocking})

    h, why = risk.halted()
    chk("Trading not halted", not h, why or "kill switch is off")
    mo = _market_open()
    chk("Market open", mo, "NSE is open" if mo else "market closed — the rule waits for the open", blocking=False)
    feed = _feed(conn, mo)
    chk("Live feed fresh", not feed["stale"], "no ticks" if feed["age_seconds"] is None
        else f"last tick {feed['age_seconds']} s ago", blocking=False)
    if long_ and env != "LIVE":
        acct = _rows(conn, "SELECT balance FROM paper_account WHERE id = 1")
        bal = _f(acct[0]["balance"]) if acct else None
        if bal is None:                     # the paper broker opens the account on its first order
            from orders.paper import settings as paper_settings
            bal = _f(paper_settings().get("paper_opening_balance"))
        chk("Funds", bal is not None and bal >= value + c_in,
            f"paper cash Rs {bal:,.0f} for Rs {value + c_in:,.0f}" if bal is not None else "paper balance unknown")
    elif long_:
        chk("Funds", True, "checked by the broker when the order is sent", blocking=False)
    circ = risk.circuit_check(conn, symbol, order_type="LIMIT", price=entry)
    chk("Inside today's circuit", not circ["breached"], circ.get("reason") or "", blocking=not circ.get("skipped"))
    pre = risk.pretrade_check(conn, symbol, side, qty, value, env=env, order_type="LIMIT", price=entry)
    chk("Risk limits", pre["ok"], pre["message"])
    if net_risk is not None and budget_left is not None:
        chk("Loss budget", net_risk <= budget_left,
            f"a stop-out costs Rs {net_risk:,.0f}; Rs {budget_left:,.0f} of today's budget is left")
    sig = signals(conn, q=symbol, limit=5)
    srow = next((r for r in sig["rows"] if r["symbol"] == symbol), None)
    if srow and srow.get("signal_price"):
        g = (entry - srow["signal_price"]) / srow["signal_price"] * 100
        chk("Entry near the signal price", abs(g) < GAP_WARN_PCT,
            f"entry is {g:+.2f}% from the {sig['date']} close (signal {srow['signal']})", blocking=False)
    if session:
        s = sessions.get_session(conn, session)
        u = sessions.usage(conn, session)
        reasons = []
        if s["status"] != "RUNNING":
            reasons.append(f"{session} is {s['status']} — the rule will wait until you resume it")
        if s["mode"] == "MANUAL":
            reasons.append(f"{session} is MANUAL — its rules never fire")
        if s.get("max_trades") and u["trades_today"] >= int(s["max_trades"]):
            reasons.append(f"{u['trades_today']} of {s['max_trades']} trades used today")
        if s.get("capital_cap") and u["open_value"] + value > float(s["capital_cap"]):
            reasons.append(f"Rs {u['open_value'] + value:,.0f} open would exceed the Rs {s['capital_cap']:,.0f} cap")
        if s.get("max_loss_value") and net_risk is not None and net_risk > float(s["max_loss_value"]):
            reasons.append(f"a stop-out (Rs {net_risk:,.0f}) is more than the session's Rs {s['max_loss_value']:,.0f} loss limit")
        chk(f"Session {session} limits", not reasons, "; ".join(reasons) or
            f"{s['mode']} · {u['trades_today']}/{s['max_trades']} trades today · Rs {u['open_value']:,.0f} open", blocking=False)
    blocked = [c for c in checks if c["blocking"] and not c["ok"]]
    return {"symbol": symbol, "side": side, "qty": qty, "entry": round(entry, 2), "target": target, "stop": stop,
            "reference_price": ref, "reference_source": ref_src, "value": round(value, 2), "env": env,
            "sizing": sizing,
            "risk_rs": round(risk_rs, 2) if risk_rs is not None else None,
            "reward_rs": round(reward_rs, 2) if reward_rs is not None else None,
            "rr": round(reward_rs / risk_rs, 2) if risk_rs and reward_rs is not None else None,
            "charges": {"entry": round(c_in, 2), "exit_at_stop": c_stop and round(c_stop, 2),
                        "exit_at_target": c_tgt and round(c_tgt, 2), "model": "nse_delivery"},
            "net_risk_rs": round(net_risk, 2) if net_risk is not None else None,
            "net_reward_rs": round(net_reward, 2) if net_reward is not None else None,
            "loss_budget": {"cap": cap, "used": lost, "left": budget_left},
            "checks": checks, "ok": not blocked,
            "message": "ready to save" if not blocked else "; ".join(c["check"] for c in blocked) + " — fix before saving"}


def save_setup(conn, b: dict) -> dict:
    """Save a Trade Setup as an ENTRY rule with its bracket, then link it to the session."""
    from orders import rules, sessions
    p = preview(conn, b.get("symbol"), b.get("side"), b.get("qty"), b.get("entry"), b.get("target"),
                b.get("stop"), b.get("session"))
    if not p["ok"]:
        raise ValueError(p["message"])
    if p["stop"] is None:
        raise ValueError("a stop is required to save a setup")
    long_ = p["side"] == "BUY"
    ref = p["reference_price"] or p["entry"]
    direction = b.get("direction") or (("BELOW" if p["entry"] <= ref else "ABOVE") if long_ else
                                       ("ABOVE" if p["entry"] >= ref else "BELOW"))
    pct = lambda x: round(abs(x - p["entry"]) / p["entry"] * 100, 3)
    payload = {"symbol": p["symbol"], "side": p["side"], "trigger_type": "PRICE", "trigger_value": p["entry"],
               "reference_price": ref, "trigger_direction": direction,
               "quantity_type": "SHARES", "quantity_value": p["qty"],
               "stoploss": {"type": "VALUE", "value": round(p["stop"] - p["entry"], 2)},
               "bracket_stop_pct": pct(p["stop"]), "bracket_target_pct": pct(p["target"]) if p["target"] else None,
               "product_type": "CNC", "order_type": "MARKET", "require_confirmation": True,
               "notes": f"Trade Setup {datetime.now():%Y-%m-%d %H:%M}" + (f" · {b['session']}" if b.get("session") else "")}
    rule = rules.create_rule(payload)
    if b.get("session"):
        sessions.link_rule(conn, b["session"], rule["id"])
    return {"rule": rule, "preview": p, "session": b.get("session") or None}


# -- UXR-08 Live Monitor -------------------------------------------------------------------------

def monitor(conn) -> dict:
    from orders import rules, sessions
    from portfolio.pnl import positions
    mo = _market_open()
    broker = _broker()
    env = "LIVE" if broker["broker_env"] == "LIVE" else "PAPER"
    try:
        active = rules.list_rules(status=rules.ACTIVE)
    except Exception:
        active = []
    live = _last_live(conn, sorted({r["symbol"] for r in active}))
    try:
        pos = positions(conn, env)
    except Exception:
        pos = []
    for p in pos:
        mine = [r for r in active if r["symbol"] == p["symbol"] and r["side"] == "SELL"]
        stop = next((r for r in mine if r.get("role") == "STOP" or r.get("trigger_direction") == "BELOW"), None)
        tgt = next((r for r in mine if r.get("role") == "TARGET" or r.get("trigger_direction") == "ABOVE"), None)
        p["stop"] = stop and stop["resolved_trigger_price"]
        p["target"] = tgt and tgt["resolved_trigger_price"]
        m = p.get("mark")
        p["to_stop_pct"] = round((m - p["stop"]) / m * 100, 2) if m and p["stop"] else None
        p["to_target_pct"] = round((p["target"] - m) / m * 100, 2) if m and p["target"] else None
        p["unrealised_pct"] = round(p["unrealised"] / p["cost"] * 100, 2) if p.get("unrealised") is not None and p["cost"] else None
    watching = []
    for r in active:
        lp, ts = live.get(r["symbol"], (None, None))
        trig = r["resolved_trigger_price"]
        est = lp * rules._resolve_quantity(r, lp) if lp and r["quantity_type"] != rules.QTY_RISK else None
        try:
            held = sessions.gate(conn, r, est)
            sid = sessions.session_of_rule(conn, r)
        except Exception as e:
            held, sid = str(e), None
        watching.append({"id": r["id"], "symbol": r["symbol"], "side": r["side"], "role": r.get("role") or "ENTRY",
                         "direction": r.get("trigger_direction"), "trigger": trig, "live": lp, "live_at": ts,
                         "distance_pct": round((trig - lp) / lp * 100, 2) if lp and trig else None,
                         "qty": r["quantity_value"], "qty_type": r["quantity_type"],
                         "confirm": bool(r["require_confirmation"]), "session": sid, "held": held,
                         "trail": bool(r.get("trail_enabled"))})
    log = _rows(conn, "SELECT id, symbol, side, role, status, quantity_value, execution_quantity, resolved_trigger_price, "
                      "trigger_hit_price, execution_price, dhan_order_id, execution_error, parent_rule_id, notes, "
                      "require_confirmation, updated_at FROM order_rules WHERE status != 'ACTIVE' "
                      "ORDER BY updated_at DESC LIMIT 25")
    for o in log:
        role = o.get("role") or "ENTRY"
        cause = {"TARGET": "target leg of a bracket", "STOP": "protective stop leg"}.get(role, "entry rule")
        if o["status"] == "EXECUTED":
            cause += " · " + ("sent without a tap (auto exit)" if not o["require_confirmation"] else "you confirmed")
        o["cause"] = cause
    fills = _rows(conn, "SELECT order_id, created_at, symbol, transaction_type, quantity, fill_price, status, reason "
                        "FROM paper_order ORDER BY created_at DESC LIMIT 10") if env == "PAPER" else []
    return {"now": datetime.now().isoformat(timespec="seconds"), "env": env, "market_open": mo,
            "feed": _feed(conn, mo), "positions": pos, "watching": watching, "log": log, "fills": fills}


# -- UXR-09 Journal & analytics ------------------------------------------------------------------

def _score_bands(conn, threshold=3.0) -> list:
    rows = _rows(conn, "SELECT l.atip_score AS s, o.hit AS hit FROM signal_log l JOIN signal_outcome o "
                       "ON o.signal_id = l.id WHERE o.threshold_pct = ? AND l.duplicate_of IS NULL "
                       "AND (o.hit = 1 OR o.still_open = 0)", (threshold,))
    bands = [("< 55", None, 55), ("55–60", 55, 60), ("60–65", 60, 65), ("65–70", 65, 70), ("70 +", 70, None)]
    out = []
    for name, lo, hi in bands:
        b = [r for r in rows if r["s"] is not None and (lo is None or r["s"] >= lo) and (hi is None or r["s"] < hi)]
        hits = sum(1 for r in b if r["hit"])
        out.append({"band": name, "signals": len(b), "hits": hits,
                    "hit_rate_pct": round(hits / len(b) * 100, 1) if b else None})
    return out


def _range_2027(conn, env) -> dict:
    rows = _rows(conn, "SELECT date, equity FROM pnl_daily WHERE env = ? AND equity IS NOT NULL ORDER BY date", (env,))
    eq = [float(r["equity"]) for r in rows]
    ch = [b - a for a, b in zip(eq, eq[1:])]
    today = date.today()
    days = max(0, round(((END_2027 - today).days) * 248 / 365))       # ~248 NSE sessions a year
    base = {"env": env, "sessions_recorded": len(eq), "sessions_to_end_2027": days,
            "note": "A straight line through your own recorded daily equity changes, widened by their spread. "
                    "Not a forecast: it assumes the next 14 months look like the last few weeks, and they will not."}
    if len(ch) < 10:
        return {**base, "ready": False, "reason": f"{len(ch)} daily changes recorded — 10 or more are needed"}
    mean = sum(ch) / len(ch)
    sd = (sum((x - mean) ** 2 for x in ch) / (len(ch) - 1)) ** 0.5
    mid = eq[-1] + mean * days
    spread = sd * math.sqrt(days)
    return {**base, "ready": True, "equity_now": round(eq[-1], 2), "avg_daily_change": round(mean, 2),
            "daily_sd": round(sd, 2), "mid": round(mid, 2), "low": round(mid - spread, 2), "high": round(mid + spread, 2)}


def journal(conn) -> dict:
    from orders import sessions
    env = "LIVE" if _broker()["broker_env"] == "LIVE" else "PAPER"
    j = sessions.journal(conn)
    tot = j["sessions"]
    closed = sum(s["closed"] for s in tot)
    wins = sum(s["successful"] for s in tot)
    return {"env": env, "sessions": tot,
            "totals": {"trades": sum(s["trades"] for s in tot), "closed": closed, "successful": wins,
                       "win_rate_pct": round(wins / closed * 100, 1) if closed else None,
                       "pnl": round(sum(s["pnl"] for s in tot), 2)},
            "score_bands": _score_bands(conn), "score_band_threshold_pct": 3.0,
            "range_2027": _range_2027(conn, env)}


# -- pages ---------------------------------------------------------------------------------------

NAV = [("/command", "Command Center"), ("/command/signals", "Signal Explorer"), ("/command/setup", "Trade Setup"),
       ("/command/monitor", "Live Monitor"), ("/command/journal", "Journal"), ("/", "Scores dashboard"),
       ("/global-asia", "Global &amp; Asia"), ("/trading", "Trading"), ("/options-builder", "Options builder")]


def _css() -> str:
    from dashboard.command_page import PAGE
    return PAGE.split("<style>", 1)[1].split("</style>", 1)[0]


EXTRA_CSS = """
.bar{height:6px;border-radius:3px;background:var(--line);overflow:hidden;min-width:80px}.bar>div{height:6px;background:var(--accent)}
.bar.inv>div{background:#FBBF24}.fgrid{display:grid;grid-template-columns:minmax(150px,1.3fr) 2fr 44px;gap:6px 10px;align-items:center;font-size:13px}
tr.sel td{background:var(--panel2)}tr[data-sym]{cursor:pointer}
.filters{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.filters input,.filters select,.form input,.form select{min-height:40px;padding:0 10px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--text);font:inherit}
.form{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}.form label{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--muted);font-weight:600}
.step{display:flex;gap:4px}.step input{flex:1;min-width:0}.step button{min-width:44px;padding:0}
.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 14px;font-size:13px}.kv b{text-align:right}
.chk{display:flex;gap:10px;padding:7px 0;border-top:1px solid var(--line);font-size:13px}.chk:first-child{border-top:none}
.chk .i{width:22px;flex:none;font-weight:700}.chk.okk .i{color:var(--good)}.chk.no .i{color:var(--bad)}.chk.soft .i{color:var(--warn)}
.track{position:relative;height:10px;border-radius:5px;background:linear-gradient(90deg,var(--badbg),var(--line),var(--goodbg));min-width:140px}
.track i{position:absolute;top:-3px;width:4px;height:16px;border-radius:2px;background:var(--text)}
.held{color:var(--warn);font-size:12px}
"""

SHELL = r"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ATIP __TITLE__</title>
<style>__CSS____EXTRA__</style></head><body>
<header>
 <div class="brand"><div class="logo" aria-hidden="true">AT</div><div>ATIP <span class="muted" style="font-weight:400">__TITLE__</span></div></div>
 <span id="pEnv" class="pill">…</span>
 <button id="killBtn" class="kill" type="button">Halt all trading</button>
</header>
__OPBANNER__
<div id="haltBanner" class="banner bad" hidden></div>
<div class="wrap">
<nav aria-label="Main">__NAV__</nav>
<main>
__BODY__
<p class="disc">Personal use only. Model outputs, not advice. Signals are computed at the close; live prices are shown for context.</p>
</main></div>
<script>
const TOKEN=__TOKEN__;
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const inr=(v,d=0)=>v==null||isNaN(v)?'—':'₹'+Number(v).toLocaleString('en-IN',{maximumFractionDigits:d,minimumFractionDigits:d});
const signed=v=>v==null?'—':(v>0?'▲ +':v<0?'▼ −':'')+inr(Math.abs(v));
const pct=v=>v==null?'—':(v>0?'▲ +':v<0?'▼ ':'')+v+'%';
async function j(u,o){const r=await fetch(u,o);let t={};try{t=await r.json()}catch(e){}if(!r.ok)throw new Error(t.error||t.detail||('HTTP '+r.status));return t}
const post=(u,b)=>j(u,{method:'POST',headers:{'Content-Type':'application/json','X-ATIP-Token':TOKEN},body:JSON.stringify(b||{})});
let HALTED=false;
async function head(){try{const d=await j('/api/command/summary');const r=d.risk||{},b=d.broker||{};HALTED=!!r.halted;
 $('pEnv').className='pill '+(b.is_live?'bad':'warn');$('pEnv').textContent=b.is_live?'LIVE · real money':(b.broker_env||'PAPER');
 const kb=$('killBtn');if(r.halted){kb.className='halted';kb.textContent='Halted · Resume on Command Center';$('haltBanner').hidden=false;$('haltBanner').textContent='Trading halted — '+(r.halt_reason||'')+'. Nothing is sent until you resume.'}
 else{kb.className='kill';kb.textContent='Halt all trading';$('haltBanner').hidden=true}}catch(e){}}
$('killBtn').onclick=async()=>{if(HALTED){location.href='/command';return}
 if(!confirm('Halt all trading?\n\nNo order will be sent, PAPER or LIVE, until you resume on the Command Center. Running sessions are paused. Open positions stay open.'))return;
 try{await post('/api/risk/halt',{reason:'Manual halt from '+document.title});}catch(e){alert(e.message)} head()};
head();setInterval(head,30000);
__SCRIPT__
</script></body></html>"""


def _page(path, title, body, script) -> str:
    from dashboard.global_asia import status_banner_html
    from dashboard.security import page_token
    nav = "".join(f'<a href="{h}"' + (' class="on" aria-current="page"' if h == path else "") + f">{t}</a>"
                  for h, t in NAV)
    out = SHELL
    for k, v in (("__CSS__", _css()), ("__EXTRA__", EXTRA_CSS), ("__NAV__", nav), ("__BODY__", body),
                 ("__SCRIPT__", script), ("__OPBANNER__", status_banner_html()), ("__TITLE__", title)):
        out = out.replace(k, v)
    return out.replace("__TOKEN__", json.dumps(page_token()))


SIGNALS_BODY = """
<div><h1>Signal Explorer</h1><div id="sub" class="muted">Loading…</div></div>
<div class="filters" role="search">
 <select id="fSig" aria-label="Signal"><option value="ALL">All signals</option><option>BUY</option><option>SELL</option><option>HOLD</option><option>WAIT</option></select>
 <input id="fQ" placeholder="Symbol" aria-label="Symbol" autocomplete="off">
</div>
<div class="row2">
 <section class="card"><h2>Ranked</h2><div class="scroll"><table><thead><tr><th>#</th><th>Stock</th><th>Signal</th><th>ATIP</th><th>Signal ₹</th><th>Live ₹</th><th>Gap</th></tr></thead><tbody id="rows"></tbody></table></div></section>
 <section class="card" aria-live="polite"><h2 id="dT">Why this signal</h2><div id="dStory" class="muted">Pick a stock.</div><div id="dF" class="fgrid" style="margin-top:12px"></div><div id="dAct" style="margin-top:12px"></div></section>
</div>"""

SIGNALS_JS = r"""
let D=null,sel=null;
function rowHtml(r){const g=r.gap_pct,big=g!=null&&Math.abs(g)>=D.gap_warn_pct;
 return `<tr data-sym="${esc(r.symbol)}" class="${sel===r.symbol?'sel':''}" tabindex="0"><td class="num muted">${esc(r.atip_rank??'')}</td><td><b>${esc(r.symbol)}</b></td><td><span class="sig ${esc(r.signal)}">${esc(r.signal)}</span></td><td class="num">${r.atip_score==null?'—':Math.round(r.atip_score)}</td><td class="num muted">${inr(r.signal_price,2)}</td><td class="num">${inr(r.live_price,2)}</td><td class="num" style="${big?'color:#FBBF24;font-weight:600':''}">${pct(g)}</td></tr>`}
function detail(sym){sel=sym;const r=D.rows.find(x=>x.symbol===sym);if(!r)return;
 document.querySelectorAll('tr[data-sym]').forEach(t=>t.classList.toggle('sel',t.dataset.sym===sym));
 $('dT').textContent='Why '+r.symbol+' · '+r.signal;$('dStory').className='';$('dStory').textContent=r.story;
 $('dF').innerHTML=r.factors.filter(f=>f.value!=null).map(f=>`<span>${esc(f.label)}</span><div class="bar ${f.inverse?'inv':''}" role="img" aria-label="${esc(f.label)} ${Math.round(f.value)} of 100"><div style="width:${Math.max(0,Math.min(100,f.value))}%"></div></div><span class="num">${Math.round(f.value)}</span>`).join('');
 const side=r.signal==='SELL'?'SELL':'BUY';
 $('dAct').innerHTML=`<a href="/command/setup?symbol=${encodeURIComponent(r.symbol)}&side=${side}"><button type="button" class="primary">Set up a ${side} →</button></a>`}
async function load(){try{D=await j('/api/command/signals?limit=150&signal='+$('fSig').value+'&q='+encodeURIComponent($('fQ').value));
 const c=D.counts||{};$('sub').textContent=D.date?('EOD '+D.date+' · '+Object.keys(c).map(k=>k+' '+c[k]).join(' · ')+' · live prices refresh every 30 s'):'No scores yet — run the post-market pipeline.';
 $('rows').innerHTML=D.rows.map(rowHtml).join('')||'<tr><td colspan="7" class="muted">No match.</td></tr>';
 if(!sel&&D.rows.length)detail(D.rows[0].symbol);else if(sel)detail(sel)}catch(e){$('sub').textContent='Could not load: '+e.message}}
$('rows').addEventListener('click',e=>{const t=e.target.closest('tr[data-sym]');if(t)detail(t.dataset.sym)});
$('rows').addEventListener('keydown',e=>{const t=e.target.closest('tr[data-sym]');if(t&&(e.key==='Enter'||e.key===' ')){e.preventDefault();detail(t.dataset.sym)}});
$('fSig').onchange=()=>{sel=null;load()};let tq;$('fQ').oninput=()=>{clearTimeout(tq);tq=setTimeout(()=>{sel=null;load()},300)};
load();setInterval(load,30000);
"""

SETUP_BODY = """
<div><h1>Trade Setup</h1><div class="muted">See the rupees at risk before a rule is saved. Saving creates a watched rule; it still asks you before any order goes out.</div></div>
<section class="card"><form id="f" class="form" onsubmit="return false">
 <label>Stock<input id="sym" required autocomplete="off" placeholder="e.g. TTML"></label>
 <label>Side<select id="side"><option>BUY</option><option>SELL</option></select></label>
 <label>Quantity (blank = size from risk)<span class="step"><button type="button" data-s="qty" data-d="-1" aria-label="Less">−</button><input id="qty" inputmode="numeric" class="num"><button type="button" data-s="qty" data-d="1" aria-label="More">+</button></span></label>
 <label>Entry ₹<span class="step"><button type="button" data-s="entry" data-d="-0.5" aria-label="Lower">−</button><input id="entry" inputmode="decimal" class="num"><button type="button" data-s="entry" data-d="0.5" aria-label="Higher">+</button></span></label>
 <label>Target ₹<span class="step"><button type="button" data-s="target" data-d="-0.5" aria-label="Lower">−</button><input id="target" inputmode="decimal" class="num"><button type="button" data-s="target" data-d="0.5" aria-label="Higher">+</button></span></label>
 <label>Stop ₹<span class="step"><button type="button" data-s="stop" data-d="-0.5" aria-label="Lower">−</button><input id="stop" inputmode="decimal" class="num"><button type="button" data-s="stop" data-d="0.5" aria-label="Higher">+</button></span></label>
 <label>Session<select id="ses"><option value="">No session</option></select></label>
</form></section>
<div class="row2">
 <section class="card" aria-live="polite"><h2>Risk and reward</h2><div id="kv" class="kv muted">Enter a stock.</div></section>
 <section class="card"><h2>Pre-trade checklist</h2><div id="chk" class="muted">—</div>
  <div class="actions" style="margin-top:12px;justify-content:flex-start"><button type="button" id="save" class="primary" disabled>Save as watched rule</button></div><div id="msg" class="sub" role="status"></div></section>
</div>"""

SETUP_JS = r"""
const P=new URLSearchParams(location.search);if(P.get('symbol'))$('sym').value=P.get('symbol');if(P.get('side'))$('side').value=P.get('side');
let last=null,t;
async function sessions(){try{const d=await j('/api/sessions');$('ses').innerHTML='<option value="">No session</option>'+d.sessions.map(s=>`<option value="${esc(s.id)}">${esc(s.id)} ${esc(s.name)} · ${esc(s.mode)} · ${esc(s.status)}</option>`).join('');if(P.get('session'))$('ses').value=P.get('session')}catch(e){}}
function qs(){const o={symbol:$('sym').value.trim(),side:$('side').value};for(const k of['qty','entry','target','stop'])if($(k).value.trim())o[k]=$(k).value.trim();if($('ses').value)o.session=$('ses').value;return o}
async function run(){const o=qs();if(!o.symbol){$('kv').textContent='Enter a stock.';return}
 try{const p=await j('/api/command/preview?'+new URLSearchParams(o));last=p;
  if(!$('entry').value)$('entry').value=p.entry;if(!$('qty').value&&p.sizing)$('qty').placeholder='sized: '+p.qty;
  const c=p.charges;$('kv').className='kv';
  $('kv').innerHTML=`<span>Quantity</span><b class="num">${p.qty}${p.sizing?' <span class="muted">(sized to '+inr(p.sizing.risk_amount)+' risk)</span>':''}</b>
   <span>Order value</span><b class="num">${inr(p.value)}</b>
   <span>Price now</span><b class="num">${inr(p.reference_price,2)} <span class="muted">${esc(p.reference_source||'')}</span></b>
   <span>At risk to the stop</span><b class="num down">${p.risk_rs==null?'set a stop':'▼ '+inr(p.risk_rs)}</b>
   <span>Reward to the target</span><b class="num up">${p.reward_rs==null?'set a target':'▲ '+inr(p.reward_rs)}</b>
   <span>Reward : risk</span><b class="num">${p.rr==null?'—':p.rr+' : 1'}</b>
   <span>Charges (in + out)</span><b class="num">${inr(c.entry,2)} + ${inr(c.exit_at_target??c.exit_at_stop,2)}</b>
   <span>Net loss if stopped</span><b class="num down">${p.net_risk_rs==null?'—':'▼ '+inr(p.net_risk_rs)}</b>
   <span>Net gain at target</span><b class="num up">${p.net_reward_rs==null?'—':'▲ '+inr(p.net_reward_rs)}</b>
   <span>Daily loss budget left</span><b class="num">${p.loss_budget.left==null?'no limit set':inr(p.loss_budget.left)}</b>`;
  $('chk').className='';$('chk').innerHTML=p.checks.map(x=>`<div class="chk ${x.ok?'okk':x.blocking?'no':'soft'}"><span class="i">${x.ok?'✓':x.blocking?'✕':'!'}</span><span><b>${esc(x.check)}</b><br><span class="muted">${esc(x.detail)}</span></span></div>`).join('');
  $('save').disabled=!(p.ok&&p.stop!=null);$('msg').textContent=p.stop==null?'Add a stop to save.':p.message}
 catch(e){last=null;$('save').disabled=true;$('kv').className='kv muted';$('kv').textContent=e.message;$('chk').textContent='—'}}
const later=()=>{clearTimeout(t);t=setTimeout(run,350)};
$('f').addEventListener('input',later);$('f').addEventListener('change',later);
$('f').addEventListener('click',e=>{const b=e.target.closest('[data-s]');if(!b)return;const i=$(b.dataset.s);const d=parseFloat(b.dataset.d);
 const base=parseFloat(i.value||(b.dataset.s==='qty'?(last?last.qty:0):(last?last.entry:0)))||0;i.value=b.dataset.s==='qty'?Math.max(1,Math.round(base+d)):(Math.max(0.05,base+d)).toFixed(2);later()});
$('save').onclick=async()=>{if(!last)return;if(!confirm('Save a watched '+last.side+' rule for '+last.qty+' '+last.symbol+' at '+inr(last.entry,2)+'?\nIt asks you before sending when it triggers.'))return;
 try{const r=await post('/api/command/setup',qs());$('msg').innerHTML='Saved rule '+esc(r.rule.id.slice(0,8))+(r.session?' in '+esc(r.session):'')+'. <a href="/command/monitor">Watch it on the Live Monitor →</a>';$('save').disabled=true}catch(e){$('msg').textContent=e.message}};
sessions().then(run);
"""

MONITOR_BODY = """
<div><h1>Live Monitor</h1><div id="sub" class="muted">Loading…</div></div>
<div id="stale" class="banner warn" role="alert" style="border-radius:8px" hidden></div>
<section class="card"><h2>Positions</h2><div class="scroll"><table><thead><tr><th>Stock</th><th>Qty</th><th>Avg ₹</th><th>Mark ₹</th><th>Stop ← price → target</th><th>P&amp;L</th></tr></thead><tbody id="pos"></tbody></table></div></section>
<section class="card"><h2>Rules watching</h2><div class="scroll"><table><thead><tr><th>Stock</th><th>Rule</th><th>Trigger ₹</th><th>Live ₹</th><th>Distance</th><th>Session</th><th>Sends</th></tr></thead><tbody id="watch"></tbody></table></div></section>
<section class="card"><h2>Order log</h2><div id="log"></div></section>"""

MONITOR_JS = r"""
function bar(p){if(p.stop==null&&p.target==null)return '<span class="muted">no stop or target rule</span>';
 const lo=p.stop??Math.min(p.avg_price,p.mark)*0.95,hi=p.target??Math.max(p.avg_price,p.mark)*1.05,x=Math.max(0,Math.min(100,(p.mark-lo)/(hi-lo)*100));
 return `<div class="track" role="img" aria-label="${p.to_stop_pct??'—'}% above the stop, ${p.to_target_pct??'—'}% below the target"><i style="left:calc(${x}% - 2px)"></i></div><div class="sub num" style="margin:2px 0 0">${inr(p.stop,2)} · ${p.to_stop_pct??'—'}% ↔ ${p.to_target_pct??'—'}% · ${inr(p.target,2)}</div>`}
async function load(){try{const d=await j('/api/command/monitor');const f=d.feed;
 $('sub').textContent=d.env+' book · market '+(d.market_open?'open':'closed')+' · live feed '+(f.age_seconds==null?'no ticks':f.age_seconds+' s ago')+' · refreshes every 10 s';
 $('stale').hidden=!f.stale;if(f.stale)$('stale').textContent='Live prices are stale while the market is open: distances below are unreliable, and auto exits refuse quotes older than 3 minutes.';
 $('pos').innerHTML=d.positions.map(p=>`<tr><td><b>${esc(p.symbol)}</b></td><td class="num">${p.qty}</td><td class="num">${inr(p.avg_price,2)}</td><td class="num">${inr(p.mark,2)}</td><td style="min-width:200px">${bar(p)}</td><td class="num ${p.unrealised>0?'up':p.unrealised<0?'down':''}">${signed(p.unrealised)} <span class="muted">${pct(p.unrealised_pct)}</span></td></tr>`).join('')||'<tr><td colspan="6" class="muted">No open positions in the '+d.env+' book.</td></tr>';
 $('watch').innerHTML=d.watching.map(w=>`<tr><td><b>${esc(w.symbol)}</b></td><td>${esc(w.role)} ${esc(w.side)} ${esc(w.qty)}${w.qty_type==='AMOUNT'?' ₹':''} <span class="muted">when ${w.direction==='ABOVE'?'≥':'≤'}</span>${w.trail?' · trailing':''}</td><td class="num">${inr(w.trigger,2)}</td><td class="num">${inr(w.live,2)}</td><td class="num">${pct(w.distance_pct)}</td><td>${esc(w.session||'—')}${w.held?'<div class="held">held: '+esc(w.held)+'</div>':''}</td><td>${w.confirm?'asks you':'<span style="color:var(--auto)">without asking</span>'}</td></tr>`).join('')||'<tr><td colspan="7" class="muted">No rules are watching.</td></tr>';
 $('log').innerHTML=d.log.map(o=>`<div class="item"><span class="num muted" style="width:110px">${esc(String(o.updated_at||'').slice(0,16).replace('T',' '))}</span><span class="grow"><b>${esc(o.symbol)}</b> ${esc(o.side)} ${esc(o.execution_quantity||o.quantity_value)} · ${esc(o.cause)}${o.execution_price?' · filled '+inr(o.execution_price,2):o.trigger_hit_price?' · hit '+inr(o.trigger_hit_price,2):''}${o.execution_error?' · <span class="down">'+esc(o.execution_error)+'</span>':''}</span><span class="pill">${esc(o.status)}</span></div>`).join('')||'<div class="muted">Nothing yet.</div>'}
 catch(e){$('sub').textContent='Could not load: '+e.message}}
load();setInterval(load,10000);
"""

JOURNAL_BODY = """
<div><h1>Journal</h1><div id="sub" class="muted">Loading…</div></div>
<section class="grid3" id="tot"></section>
<section class="card"><h2>By session</h2><div class="scroll"><table><thead><tr><th>Session</th><th>Mode</th><th>Trades</th><th>Closed</th><th>Successful</th><th>Win rate</th><th>Avg R</th><th>P&amp;L</th></tr></thead><tbody id="ses"></tbody></table></div>
 <div class="sub">Counts come from order rules linked to a session (Trade Setup links them) and the bracket legs they spawn.</div></section>
<div class="row2">
 <section class="card"><h2>Hit rate by ATIP score band</h2><div class="scroll"><table><thead><tr><th>Score</th><th>Settled signals</th><th>Reached +<span id="thr"></span>%</th><th>Hit rate</th></tr></thead><tbody id="bands"></tbody></table></div></section>
 <section class="card"><h2>Straight-line range to Dec 2027</h2><div id="rng"></div></section>
</div>"""

JOURNAL_JS = r"""
async function load(){try{const d=await j('/api/command/journal');const t=d.totals;
 $('sub').textContent=d.env+' book · every session, all time';
 $('tot').innerHTML=[['Trades',t.trades],['Closed · successful',t.closed+' · '+t.successful],['Win rate',t.win_rate_pct==null?'—':t.win_rate_pct+'%'],['P&L',signed(t.pnl)]].map(x=>`<div class="card"><div class="lbl">${x[0]}</div><div class="big num">${x[1]}</div></div>`).join('');
 $('ses').innerHTML=d.sessions.map(s=>`<tr><td><b>${esc(s.id)}</b> ${esc(s.name)} <span class="muted">${esc(s.status)}</span></td><td><span class="mode ${esc(s.mode)}">${esc(s.mode)}</span></td><td class="num">${s.trades}</td><td class="num">${s.closed}</td><td class="num">${s.successful}</td><td class="num">${s.win_rate_pct==null?'—':s.win_rate_pct+'%'}</td><td class="num">${s.avg_r==null?'—':s.avg_r}</td><td class="num ${s.pnl>0?'up':s.pnl<0?'down':''}">${signed(s.pnl)}</td></tr>`).join('');
 $('thr').textContent=d.score_band_threshold_pct;
 $('bands').innerHTML=d.score_bands.map(b=>`<tr><td>${esc(b.band)}</td><td class="num">${b.signals}</td><td class="num">${b.hits}</td><td class="num">${b.hit_rate_pct==null?'—':b.hit_rate_pct+'%'}</td></tr>`).join('');
 const r=d.range_2027;
 $('rng').innerHTML=(r.ready?`<div class="kv"><span>Equity now</span><b class="num">${inr(r.equity_now)}</b><span>Average daily change</span><b class="num">${signed(r.avg_daily_change)}</b><span>Sessions to 31 Dec 2027</span><b class="num">${r.sessions_to_end_2027}</b><span>Range</span><b class="num">${inr(r.low)} – ${inr(r.high)}</b><span>Middle of the line</span><b class="num">${inr(r.mid)}</b></div>`:`<div class="muted">${esc(r.reason)}.</div>`)+`<p class="sub">${esc(r.note)}</p>`}
 catch(e){$('sub').textContent='Could not load: '+e.message}}
load();
"""

PAGES = {"/command/signals": ("Signal Explorer", SIGNALS_BODY, SIGNALS_JS),
         "/command/setup": ("Trade Setup", SETUP_BODY, SETUP_JS),
         "/command/monitor": ("Live Monitor", MONITOR_BODY, MONITOR_JS),
         "/command/journal": ("Journal", JOURNAL_BODY, JOURNAL_JS)}


def register(app, guard, Req, get_connection, json_safe):
    def call(fn):
        conn = get_connection()
        try:
            return JSONResponse(_finite(json_safe(fn(conn))))
        except LookupError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        except (ValueError, TypeError, KeyError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        finally:
            conn.close()

    def page(path):
        title, body, script = PAGES[path]

        async def view():
            return HTMLResponse(_page(path, title, body, script))
        app.add_api_route(path, view, methods=["GET"], response_class=HTMLResponse)

    for path in PAGES:
        page(path)

    @app.get("/api/command/signals")
    async def api_command_signals(signal: str = "ALL", q: str = "", limit: int = 60):
        return await run_in_threadpool(call, lambda c: signals(c, signal, q, limit))

    @app.get("/api/command/preview")
    async def api_command_preview(symbol: str = "", side: str = "BUY", qty: str = None, entry: str = None,
                                  target: str = None, stop: str = None, session: str = None):
        return await run_in_threadpool(call, lambda c: preview(c, symbol, side, qty, entry, target, stop, session))

    @app.post("/api/command/setup", dependencies=guard)
    async def api_command_setup(request: Req):
        try:
            b = await request.json()
        except Exception:
            b = {}
        b = b if isinstance(b, dict) else {}
        return await run_in_threadpool(call, lambda c: save_setup(c, b))

    @app.get("/api/command/monitor")
    async def api_command_monitor():
        return await run_in_threadpool(call, monitor)

    @app.get("/api/command/journal")
    async def api_command_journal():
        return await run_in_threadpool(call, journal)
