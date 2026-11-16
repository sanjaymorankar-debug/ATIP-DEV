"""
Paper capital plan (owner prompt 16 Nov 2026, 16:39 IST: "put the idle capital to work ... config-driven, no
hardcoded amounts ... keep a cash buffer of at least 10 % ... sessions cannot over-allocate together").

THE PROBLEM IT SOLVES. The paper account opened with Rs 5,00,000 (orders/paper.py paper_opening_balance); the ten
sessions S1-S10 were given Rs 25,000 each on 29 Oct (the owner's 10 x Rs 25,000 plan), so half of the account never
belonged to any session and sat in cash. On 16 Nov at 16:41 IST the paper book held Rs 3,27,565 of cash and
Rs 1,82,035 (cost) of stock: 36 % of its Rs 5.10 lakh equity at work.

WHAT IT DOES (the recommended option (a), "raise the allocation of the existing sessions"). Every RUNNING pool session
gets the SAME top-up out of the cash that belongs to no session (orders/sessions.py spare_paper_cash), moved by
accounting/ledger.py transfer -- the SES-03 transfer from the unallocated pot. A transfer restarts no books, touches no
open trade and changes no rule: the session's next occurrence simply has more cash, and a risk-budget arm
(orders/arms.py, 2.5 % of the cash at the stop) sizes its next buy from it. Equal amounts keep the ten arms comparable
for the weekly A/B report and keep the sessions independent (a session's own profit or loss is never shared).

    top-up per session = round_down( (spare - buffer) / sessions, round_to ), never past max_session_pct of the pool
    buffer             = cash_buffer_pct of the paper pool (cash + open paper positions at their marks)

CONFIG (atip_data/config.json "paper_capital_plan"; every figure a share or a count, never a rupee amount):

    cash_buffer_pct         10        share of the paper pool that no session may be given
    max_session_pct         10        no session's capital (start + transfers) above this share of the pool
    sessions                "running" which sessions are topped up: "running" (RUNNING pool sessions) or a list
    round_to                1000      each top-up is rounded down to this many rupees
    session_loss_limit_pct  6         a topped-up session's daily loss limit (trading_sessions.max_loss_value) as a
                                      share of its capital: Rs 1,500 on Rs 25,000 today; null leaves the limits alone
    pool                    "equity"  "equity" (paper cash + open positions at marks) or "opening" (the opening balance)

THE GUARD (allocation_check): once the block is in config.json, nothing hands a session paper cash beyond the
headroom -- spare cash less the buffer -- or past max_session_pct: a transfer in from the unallocated pot
(accounting/ledger.py), a pool (re)start (orders/sessions.py start_pool / setup_pools) and a new session
(add_session). Without the block only the hard limit applies to a transfer in: never more than the spare cash.
At order time the paper broker's own funds check (orders/paper.py, the whole paper balance) still refuses a buy the
account cannot pay for.

PAPER ONLY. With any other broker_env plan() describes nothing and apply() refuses; the viewer refuses apply().

    python -m orders.capital_plan              # the plan: before, the top-ups, after, the guardrails (writes nothing)
    python -m orders.capital_plan --apply --ack   # move the top-ups (PAPER), logged in session_events and the ledger
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from pathlib import Path

log = logging.getLogger("atip.capital_plan")

CONFIG_PATH = Path("atip_data") / "config.json"
KEY = "paper_capital_plan"
DEFAULTS = {"cash_buffer_pct": 10.0, "max_session_pct": 10.0, "sessions": "running", "round_to": 1000.0,
            "session_loss_limit_pct": 6.0, "pool": "equity"}
SETTING = "capital_plan:last"           # session_settings: the last applied plan (JSON)
EVENT = "CAPITAL_PLAN"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def configured() -> dict | None:
    """The owner's plan block merged over the defaults, or None when config.json has no block (the guard is off)."""
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get(KEY)
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    out = dict(DEFAULTS)
    out.update({k: v for k, v in raw.items() if not str(k).startswith("_")})
    for k in ("cash_buffer_pct", "max_session_pct", "round_to"):
        try:
            out[k] = float(out[k])
        except (TypeError, ValueError):
            raise ValueError(f"{KEY}.{k} must be a number")
        if out[k] < 0 or (k == "round_to" and out[k] <= 0) or (k != "round_to" and out[k] > 100):
            raise ValueError(f"{KEY}.{k} is out of range: {out[k]}")
    if out.get("session_loss_limit_pct") not in (None, ""):
        out["session_loss_limit_pct"] = float(out["session_loss_limit_pct"])
    else:
        out["session_loss_limit_pct"] = None
    if out.get("pool") not in ("equity", "opening"):
        raise ValueError(f"{KEY}.pool must be \"equity\" or \"opening\"")
    return out


def settings() -> dict:
    """configured() or the defaults (for the plan's description when the block is missing)."""
    return configured() or dict(DEFAULTS)


def _paper() -> bool:
    from orders import sessions
    return sessions._paper()


def pool(conn, cfg: dict | None = None) -> dict:
    """The paper pool the shares are taken of: cash + open paper positions at their marks (positions with no mark
    at cost), or the opening balance (cfg pool = "opening")."""
    cfg = cfg or settings()
    from orders.paper import settings as paper_settings
    opening = float(paper_settings()["paper_opening_balance"])
    try:
        from portfolio.pnl import portfolio_summary
        s = portfolio_summary(conn, "PAPER")
        cash = s["cash"]
        held = sum(p["value"] if p["value"] is not None else p["cost"] for p in s["positions"])
        cost = float(s["cost"] or 0)
    except Exception as e:
        log.warning(f"  paper book unreadable for the capital plan ({e}): using the opening balance")
        cash, held, cost = None, 0.0, 0.0
    if cash is None:
        cash = opening
    equity = round(float(cash) + held, 2)
    value = opening if cfg.get("pool") == "opening" else equity
    return {"basis": cfg.get("pool"), "value": round(value, 2), "opening": round(opening, 2), "cash": round(float(cash), 2),
            "positions_value": round(held, 2), "positions_cost": round(cost, 2), "equity": equity}


def headroom(conn, cfg: dict | None = None) -> dict:
    """What more the sessions may be given: spare paper cash less the buffer (never below 0)."""
    from orders import sessions
    cfg = cfg or settings()
    sp = sessions.spare_paper_cash(conn)
    p = pool(conn, cfg)
    buffer = round(p["value"] * cfg["cash_buffer_pct"] / 100, 2)
    return {"spare": sp["spare"], "over_allocated": sp.get("over_allocated", 0.0), "buffer": buffer,
            "buffer_pct": cfg["cash_buffer_pct"], "headroom": round(max(0.0, sp["spare"] - buffer), 2),
            "max_session": round(p["value"] * cfg["max_session_pct"] / 100, 2), "pool": p, "sessions_cash": sp["sessions_cash"]}


def _capital(s: dict) -> float:
    """A pool session's capital: its start amount plus every transfer in (less out) since the pool started."""
    L = s.get("pool") or {}
    return float(L.get("start_capital") or s.get("start_capital") or 0) + float(L.get("transfers") or 0)


def allocation_check(conn, sid: str | None, new_money: float, what: str = "this") -> None:
    """Raise ValueError when giving `new_money` of paper cash to `sid` (None = a session not made yet) would over-allocate
    the paper pool: more than the spare cash (always, PAPER), or -- with the plan block in config.json -- more than the
    headroom left above the buffer, or a session above max_session_pct of the pool. Outside PAPER: nothing checked."""
    new_money = round(float(new_money or 0), 2)
    if new_money <= 0 or not _paper():
        return
    from orders import sessions
    cfg = configured()
    sp = sessions.spare_paper_cash(conn)
    if new_money > sp["spare"] + 0.005:
        raise ValueError(f"{what}: Rs {new_money:,.2f} is more than the Rs {sp['spare']:,.2f} of paper cash that belongs "
                         f"to no session -- the sessions together would hold more than the paper account")
    if not cfg:
        return
    h = headroom(conn, cfg)
    if new_money > h["headroom"] + 0.005:
        raise ValueError(f"{what}: Rs {new_money:,.2f} would leave less than the {cfg['cash_buffer_pct']:g} % cash buffer "
                         f"(Rs {h['buffer']:,.2f} of the Rs {h['pool']['value']:,.2f} paper pool); at most "
                         f"Rs {h['headroom']:,.2f} can be given to sessions ({KEY}.cash_buffer_pct)")
    if sid and cfg["max_session_pct"]:
        try:
            s = sessions.get_session(conn, sid)
        except LookupError:
            return
        s["pool"] = sessions.ledger(conn, sid, s=s) if s.get("start_capital") else None
        after = _capital(s) + new_money
        if after > h["max_session"] + 0.005:
            raise ValueError(f"{what}: session {sid} would have Rs {after:,.2f}, more than {cfg['max_session_pct']:g} % of "
                             f"the paper pool (Rs {h['max_session']:,.2f}, {KEY}.max_session_pct)")


def _targets(conn, cfg: dict) -> list:
    from orders import sessions
    every = [s for s in sessions.list_sessions(conn)["sessions"] if s.get("pool")]
    want = cfg.get("sessions")
    if isinstance(want, (list, tuple)):
        ids = {str(x).upper() for x in want}
        return [s for s in every if s["id"] in ids]
    return [s for s in every if s["status"] == "RUNNING"]


def _size_ratio(conn, s: dict, cash: float) -> float | None:
    """The share of a session's cash its next buy uses: a risk-budget arm buys max(floor, 2.5 % of cash / stop %),
    the control and plain pools their whole cash. An ATR stop is taken at the middle of its band."""
    if cash <= 0:
        return None
    try:
        from orders import arms
        arm = arms.get(conn, s["id"])
    except Exception:
        arm = None
    stop = (arm or {}).get("stop") or {}
    if stop.get("type") == "pct":
        pct = float(stop.get("value") or 0)
    elif stop.get("type") == "atr":
        pct = (float(stop.get("min") or 0) + float(stop.get("max") or 0)) / 2
    else:
        pct = float(s.get("stop_pct") or 0)
    floor = float((s.get("pool") or {}).get("floor") or s.get("min_buy_value") or 0)
    from orders import arms as A
    cap = A.value_cap(arm, pct, floor, cash) if arm else None
    return round(min(1.0, (cap if cap is not None else cash) / cash), 4)


def plan(conn) -> dict:
    """Before, the top-up of each session, after, and the guardrails. Writes nothing."""
    cfg = configured()
    active = cfg is not None
    cfg = cfg or dict(DEFAULTS)
    if not _paper():
        return {"paper": False, "reason": "broker_env is not PAPER: the capital plan is PAPER only", "rows": []}
    h = headroom(conn, cfg)
    targets = _targets(conn, cfg)
    n = len(targets)
    step = float(cfg["round_to"])
    per = math.floor(h["headroom"] / n / step) * step if n else 0.0
    rows, given = [], 0.0
    for s in targets:
        L = s["pool"]
        equity, cash = float(L["equity"]), float(L["cash"])
        room = max(0.0, h["max_session"] - _capital(s)) if cfg["max_session_pct"] else per
        top = min(per, math.floor(room / step) * step)
        given += top
        cap_after = _capital(s) + top
        loss_after = (round(cap_after * cfg["session_loss_limit_pct"] / 100, 2)
                      if cfg.get("session_loss_limit_pct") else s.get("max_loss_value"))
        cost = sum(float(o.get("cost") or 0) for o in L["open"])
        # what the session has for its next buy: its cash now, plus (at cost) what its open trade returns when it exits
        money_before, money_after = cash + cost, cash + cost + top
        ratio_b, ratio = _size_ratio(conn, s, money_before), _size_ratio(conn, s, money_after)
        rows.append({"session": s["id"], "name": s["name"], "status": s["status"], "capital_before": round(_capital(s), 2),
                     "equity_before": round(equity, 2), "cash_before": round(cash, 2),
                     "deployed_cost": round(sum(float(o.get("cost") or 0) for o in L["open"]), 2),
                     "open": [o["symbol"] for o in L["open"]], "top_up": round(top, 2),
                     "capital_after": round(cap_after, 2), "cash_after": round(cash + top, 2),
                     "equity_after": round(equity + top, 2), "loss_limit_before": s.get("max_loss_value"),
                     "loss_limit_after": loss_after, "next_buy_share": ratio,
                     "next_buy_value_before": round(money_before * ratio_b, 2) if ratio_b is not None else None,
                     "next_buy_value": round(money_after * ratio, 2) if ratio is not None else None,
                     "next_buy_when": "now (no open trade)" if not L["open"] else f"after {', '.join(o['symbol'] for o in L['open'])} exits"})
    p = h["pool"]
    sess_eq_before = round(sum(r["equity_before"] for r in rows), 2)
    deployed = p["positions_cost"]

    def _full(key):                                    # every session in a trade at once, each sized by its own rule
        return round(sum(float(r[key] or 0) for r in rows), 2)
    before_full, after_full = _full("next_buy_value_before"), _full("next_buy_value")
    pv = p["value"] or 1.0
    return {"paper": True, "guard_active": active, "config": cfg, "as_of": _now(), "pool": p,
            "spare_before": h["spare"], "buffer": h["buffer"], "headroom": h["headroom"], "max_session": h["max_session"],
            "over_allocated": h["over_allocated"], "sessions": n, "per_session": per, "rows": rows,
            "total_top_up": round(given, 2), "spare_after": round(h["spare"] - given, 2),
            "buffer_after_pct": round((h["spare"] - given) / pv * 100, 2),
            "before": {"sessions_capital": round(sum(r["capital_before"] for r in rows), 2), "sessions_equity": sess_eq_before,
                       "allocated_pct": round(sess_eq_before / pv * 100, 2), "deployed": deployed,
                       "deployed_pct": round(deployed / pv * 100, 2), "full_occupancy_deployed": before_full,
                       "full_occupancy_pct": round(before_full / pv * 100, 2)},
            "after": {"sessions_capital": round(sum(r["capital_after"] for r in rows), 2),
                      "sessions_equity": round(sess_eq_before + given, 2),
                      "allocated_pct": round((sess_eq_before + given) / pv * 100, 2), "deployed": deployed,
                      "deployed_pct": round(deployed / pv * 100, 2), "full_occupancy_deployed": after_full,
                      "full_occupancy_pct": round(after_full / pv * 100, 2)},
            "guardrails": guardrails(conn, cfg, rows, h)}


def guardrails(conn, cfg: dict, rows: list, h: dict) -> dict:
    """The limits that bound the plan, in rupees at today's pool: per trade, per session, open positions, daily loss,
    and the cash buffer."""
    try:
        from orders.risk import limits
        lim = limits()
    except Exception:
        lim = {}
    return {"cash_buffer": {"pct": cfg["cash_buffer_pct"], "rupees": h["buffer"]},
            "max_per_session": {"pct": cfg["max_session_pct"], "rupees": h["max_session"]},
            "max_per_trade": "the session's own cash: a risk-budget arm risks 2.5 % of it at the stop "
                             "(orders/arms.py); session orders are exempt from max_order_value "
                             f"(Rs {float(lim.get('max_order_value') or 0):,.0f}) but never exceed the session's cash",
            "max_per_trade_rupees": max((r["next_buy_value"] or 0) for r in rows) if rows else None,
            "max_open_positions": {"per_session": 1, "sessions": len(rows), "account": lim.get("max_open_positions")},
            "daily_loss": {"per_session_pct": cfg.get("session_loss_limit_pct"),
                           "per_session_rupees": {r["session"]: r["loss_limit_after"] for r in rows},
                           "account_rupees": lim.get("max_daily_loss_value"), "drawdown_pct": lim.get("max_drawdown_pct")},
            "orders_per_day": lim.get("max_orders_per_day")}


def apply(conn, ack: bool = False, reason: str = "") -> dict:
    """Move each session's top-up from the unallocated pot (PAPER). ack=True is required. A second run moves only what
    the headroom still allows (nothing, once applied)."""
    from ops.role import refuse
    refuse("Capital plan")
    if not ack:
        raise ValueError("the capital plan moves paper cash into the sessions: confirm with ack=true")
    if not _paper():
        raise ValueError("the capital plan is PAPER only: broker_env is not PAPER")
    p = plan(conn)
    from accounting.ledger import transfer
    from orders import sessions
    why = (reason or "").strip() or f"capital plan {datetime.now():%d %b %Y}: idle paper cash to the sessions"
    done, skipped = [], {}
    for r in p["rows"]:
        if r["top_up"] <= 0:
            continue
        try:
            t = transfer(conn, None, r["session"], r["top_up"], why)
            done.append({"session": r["session"], "amount": r["top_up"], "transfer_id": t["transfer_id"]})
            if p["config"].get("session_loss_limit_pct") and r["loss_limit_after"]:
                sessions.update_session(conn, r["session"], {"max_loss_value": r["loss_limit_after"]})
        except (ValueError, LookupError) as e:
            skipped[r["session"]] = str(e)
    summary = {"at": _now(), "moved": round(sum(d["amount"] for d in done), 2), "sessions": done, "skipped": skipped,
               "spare_before": p["spare_before"], "buffer": p["buffer"], "per_session": p["per_session"]}
    if done:
        sessions._event(conn, None, EVENT, summary)
        conn.execute("INSERT INTO session_settings (name, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE "
                     "SET value = excluded.value, updated_at = excluded.updated_at", (SETTING, json.dumps(summary), _now()))
        conn.commit()
    return {**summary, "after": plan(conn)}


def _print(p: dict) -> None:
    if not p.get("paper"):
        print(p.get("reason")); return
    pl = p["pool"]
    print(f"Paper pool Rs {pl['value']:,.2f} ({pl['basis']}): cash Rs {pl['cash']:,.2f}, positions Rs {pl['positions_value']:,.2f}")
    print(f"Spare (no session's) Rs {p['spare_before']:,.2f}; buffer {p['config']['cash_buffer_pct']:g} % = Rs {p['buffer']:,.2f}; "
          f"headroom Rs {p['headroom']:,.2f}; guard {'ON' if p['guard_active'] else 'OFF (no config block)'}")
    print(f"{'S':5} {'capital':>11} {'cash':>11} {'top-up':>9} {'capital→':>11} {'loss lim→':>9} {'next buy':>10}  when")
    for r in p["rows"]:
        print(f"{r['session']:5} {r['capital_before']:>11,.2f} {r['cash_before']:>11,.2f} {r['top_up']:>9,.0f} "
              f"{r['capital_after']:>11,.2f} {float(r['loss_limit_after'] or 0):>9,.0f} {float(r['next_buy_value'] or 0):>10,.0f}  {r['next_buy_when']}")
    b, a = p["before"], p["after"]
    print(f"Allocated to sessions {b['allocated_pct']}% -> {a['allocated_pct']}%; deployed now {b['deployed_pct']}%; "
          f"all sessions in a trade {b['full_occupancy_pct']}% -> {a['full_occupancy_pct']}%; spare after Rs {p['spare_after']:,.2f} "
          f"({p['buffer_after_pct']}%)")


if __name__ == "__main__":
    import argparse
    from db.schema import get_connection
    ap = argparse.ArgumentParser(prog="python -m orders.capital_plan", description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="move the top-ups (PAPER)")
    ap.add_argument("--ack", action="store_true", help="confirm the move")
    ap.add_argument("--reason", default="")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    c = get_connection()
    try:
        out = apply(c, ack=a.ack, reason=a.reason) if a.apply else plan(c)
        if a.json:
            print(json.dumps(out, indent=1, default=str))
        elif a.apply:
            print(f"moved Rs {out['moved']:,.2f} to {len(out['sessions'])} session(s); skipped {out['skipped'] or 'none'}")
            _print(out["after"])
        else:
            _print(out)
    finally:
        c.close()
