"""
Entry guards from the 6 Nov 2026 post-market RCA (portfolio/post_market_rca.py). EVERY ONE IS OFF until the owner turns
it on (owner prompt 6 Nov 2026: "keep risk rules confirmation-gated; do not enable any new auto-execution without my
approval"); with all of them off auto-pick behaves exactly as before.

    name                      value      what it does once on                       42 sessions, 420 picks (6 Nov study)
    guard_orb_minutes         30         auto-pick buys only once the price is      -0.31 % a trade vs -0.64 %, better on
                                         above the first 30 minutes' high, after    30 of 42 days, both halves of the
                                         09:45 IST (opening-range breakout)         period (205 of 420 trades)
    guard_min_score           63         auto-pick skips a rank-fill / HOLD stock   -0.45 % vs -0.64 %, better 21 / worse 7
                                         scoring under this                         days; with the ORB guard ~+0.05 %
    guard_nifty_weak          ON         no auto-pick while Nifty is under its      -0.44 % vs -0.64 %, better 19 / worse 5
                                         first print of the day or 0.5 %+ down
    guard_entry_not_before    09:45      no auto-pick before this time (IST)        -0.43 % vs -0.64 %, 26 / 16 days
    guard_sector_cap          1          skip a stock whose industry this many      not back-tested (a risk cap)
                                         sessions already hold / queue

Not here, on purpose: a stop at k x ATR14 with the same shares. It turned 6 Nov's -Rs 763 into +Rs 1,641, but over the
42 sessions it lost 2.4 times more than the 2 % stop (-1.54 % a trade). tools/rca_validate_rules.py re-runs the study.

Turning one on needs ack=True (the /rca page's dialog, or --ack on the command line) and is logged as a session event;
turning one off needs nothing. The view-only server refuses every change (ops/role.py).

    python -m orders.entry_guards                          # each guard and its state
    python -m orders.entry_guards --set guard_orb_minutes 30 --ack
    python -m orders.entry_guards --set guard_orb_minutes off
"""

from __future__ import annotations

import logging
from datetime import date, datetime

log = logging.getLogger("atip.guards")

GUARDS = {   # name: (label, kind, suggested value)
    "guard_orb_minutes": ("Buy only above the first N minutes' high (opening-range breakout)", "int", 30),
    "guard_min_score": ("Skip rank-fill / HOLD stocks scoring under X", "float", 63.0),
    "guard_nifty_weak": ("No auto-pick while Nifty is under its open or 0.5 %+ down", "flag", "ON"),
    "guard_entry_not_before": ("No auto-pick before HH:MM IST", "time", "09:45"),
    "guard_sector_cap": ("At most N sessions in one industry", "int", 1),
}
NIFTY_WEAK_PCT = -0.5
_ORH: dict = {}                     # (day, symbol, minutes) -> the opening range's high, once known
OFF = "OFF"


def _row(r):
    return dict(r) if hasattr(r, "keys") else r


def settings(conn) -> dict:
    """{name: value or None (off)} for every guard."""
    from orders.sessions import ensure_tables
    ensure_tables(conn)
    out = {k: None for k in GUARDS}
    for r in conn.execute("SELECT name, value FROM session_settings WHERE name LIKE 'guard_%'").fetchall():
        r = _row(r)
        if r["name"] in out and str(r["value"]).strip().upper() != OFF:
            out[r["name"]] = _parse(r["name"], r["value"])
    return out


def _parse(name: str, value):
    kind = GUARDS[name][1]
    v = str(value).strip()
    if kind == "flag":
        return "ON" if v.upper() in ("ON", "1", "TRUE", "YES") else None
    if kind == "time":
        h, m = v.split(":")
        if not (0 <= int(h) < 24 and 0 <= int(m) < 60):
            raise ValueError(f"{name}: a time HH:MM (IST)")
        return f"{int(h):02d}:{int(m):02d}"
    x = float(v)
    if x <= 0 or (name == "guard_min_score" and x > 100) or (name == "guard_orb_minutes" and (x > 120 or x % 15)):
        raise ValueError(f"{name}: {v} is out of range")
    return int(x) if kind == "int" else x


def set_guard(conn, name: str, value, ack: bool = False, reason: str = "") -> dict:
    """Turn a guard on at `value` (needs ack=True) or off (value None / "" / "off")."""
    from ops.role import refuse
    from orders import sessions
    refuse("changing a trading guard")
    if name not in GUARDS:
        raise ValueError(f"no guard {name!r}; one of {', '.join(GUARDS)}")
    off = str(value if value is not None else "").strip().lower() in ("", "off", "none")
    v = None if off else _parse(name, value)
    if v is not None and not ack:
        raise PermissionError(f"turning {name} on changes what auto-pick buys: confirm it (ack)")
    was = settings(conn)[name]
    conn.execute("INSERT OR REPLACE INTO session_settings (name, value, updated_at) VALUES (?, ?, ?)",
                 (name, OFF if v is None else str(v), sessions._now()))
    sessions._event(conn, None, "GUARD", {"guard": name, "from": was, "to": v, "reason": reason})
    conn.commit()
    log.info(f"  guard {name}: {was} -> {v} ({reason or 'no reason given'})")
    return {"guard": name, "value": v, "was": was}


def status(conn) -> list:
    s = settings(conn)
    return [{"name": k, "label": lbl, "kind": kind, "value": s[k], "on": s[k] is not None, "suggested": sug}
            for k, (lbl, kind, sug) in GUARDS.items()]


# -- the checks ----------------------------------------------------------------------------------------------------

def _nifty(conn, day: str) -> dict:
    """{first, now, chg}: the day's first Nifty 50 print at or after 09:15, the latest one and its change (index_levels)."""
    try:
        first = conn.execute("SELECT nifty50 FROM index_levels WHERE date = ? AND time >= '09:15' ORDER BY time LIMIT 1",
                             (day,)).fetchone()
        last = conn.execute("SELECT nifty50, nifty50_chg FROM index_levels WHERE date = ? ORDER BY time DESC LIMIT 1",
                            (day,)).fetchone()
    except Exception:
        return {}
    if not first or not last:
        return {}
    return {"first": _row(first)["nifty50"], "now": _row(last)["nifty50"], "chg": _row(last)["nifty50_chg"]}


_IND: dict = {}


def _industry(symbol: str):
    """The stock's NSE industry; the map is read once a day (a stale cache refreshes at most then)."""
    day = str(date.today())
    if _IND.get("day") != day:
        try:
            from data.index_constituents import get_symbol_industry_map
            _IND.update(day=day, map=get_symbol_industry_map() or {})
        except Exception:
            _IND.update(day=day, map={})
    return _IND["map"].get(symbol)


def check_entry(conn, cand: dict, busy=(), now: datetime | None = None) -> str | None:
    """Why auto-pick should pass over `cand` ({symbol, score, signal, source}) under the guards that are on, or None.
    `busy`: the symbols the sessions already hold or have queued (orders/autopick.py _busy)."""
    g = settings(conn)
    if not any(v is not None for v in g.values()):
        return None
    now = now or datetime.now()
    day = str(now.date())
    if g["guard_entry_not_before"] and now.strftime("%H:%M") < g["guard_entry_not_before"]:
        return f"guard: no auto-pick before {g['guard_entry_not_before']} IST"
    if g["guard_min_score"] and cand.get("signal") != "BUY" and (cand.get("score") or 0) < g["guard_min_score"]:
        return f"guard: {cand['symbol']} scores {cand.get('score')} (< {g['guard_min_score']:g})"
    if g["guard_nifty_weak"]:
        n = _nifty(conn, day)
        if n and ((n["now"] or 0) < (n["first"] or 0) or (n["chg"] is not None and n["chg"] <= NIFTY_WEAK_PCT)):
            # LRCA-17 (20 Nov 2026): the feed stores an unknown change as NULL (LRCA-13); formatting None raised
            # TypeError out of check_entry and failed every auto-pick pass
            chg = f"{n['chg']:+.2f}%" if n["chg"] is not None else "change n/a"
            first = f"{n['first']:,.2f}" if n["first"] is not None else "n/a"
            return f"guard: Nifty is weak ({chg}, under its first print {first})"
    if g["guard_orb_minutes"]:
        n = int(g["guard_orb_minutes"])
        end = f"{9 + (15 + n) // 60:02d}:{(15 + n) % 60:02d}"
        if now.strftime("%H:%M") < end:
            return f"guard: waiting for the {n}-minute opening range (until {end} IST)"
        orh = opening_high(conn, cand["symbol"], day, n)
        if orh is None:
            return f"guard: no {n}-minute opening range for {cand['symbol']} yet"
        if not cand.get("price") or cand["price"] <= orh:
            return f"guard: {cand['symbol']} at {cand.get('price')} is not above its opening-range high {orh:,.2f}"
    if g["guard_sector_cap"]:
        ind = _industry(cand["symbol"])
        if ind:
            same = sorted(s for s in busy if s != cand["symbol"] and _industry(s) == ind)
            if len(same) >= g["guard_sector_cap"]:
                return f"guard: {ind} already held / queued ({', '.join(same)})"
    return None


def opening_high(conn, symbol: str, day: str, minutes: int = 30) -> float | None:
    """The high of the first `minutes` (a multiple of 15) of `day`: the stored 15-minute bars, else one 15-minute
    fetch from Dhan on the trading Mac (cached for the day); None when neither has it yet."""
    key = (day, symbol, minutes)
    if key in _ORH:
        return _ORH[key]
    end = f"{day} {9 + (15 + minutes) // 60:02d}:{(15 + minutes) % 60:02d}:00"
    hi = None
    try:
        rows = conn.execute("SELECT high FROM intraday_bars WHERE symbol = ? AND interval_min = 15 AND ts >= ? AND ts < ?",
                            (symbol, f"{day} 09:15:00", end)).fetchall()
        if len(rows) >= minutes // 15:
            hi = max(float(_row(r)["high"]) for r in rows)
    except Exception:
        hi = None
    if hi is None:
        try:
            from ops.role import is_viewer
            if not is_viewer():
                from data import dhan as D
                d = date.fromisoformat(day)
                df = D.fetch_historical_intraday(symbol, d, d, interval=15)
                if len(df):
                    part = df[df["datetime"].astype(str) < end]
                    if len(part) >= minutes // 15:
                        hi = float(part["high"].max())
        except Exception as e:
            log.warning(f"  guard ORB: {symbol} opening range: {e}")
    if hi is not None:
        _ORH[key] = round(hi, 2)
    return _ORH.get(key)


if __name__ == "__main__":
    import argparse
    from db.schema import get_connection
    ap = argparse.ArgumentParser(description="RCA entry / stop guards (all OFF by default)")
    ap.add_argument("--set", nargs=2, metavar=("NAME", "VALUE"))
    ap.add_argument("--ack", action="store_true", help="confirm turning a guard on")
    ap.add_argument("--reason", default="command line")
    a = ap.parse_args()
    c = get_connection()
    try:
        if a.set:
            print(set_guard(c, a.set[0], a.set[1], ack=a.ack, reason=a.reason))
        for s in status(c):
            print(f"  {s['name']:26s} {'ON  ' + str(s['value']) if s['on'] else 'off':12s} {s['label']} "
                  f"(suggested {s['suggested']})")
    finally:
        c.close()
