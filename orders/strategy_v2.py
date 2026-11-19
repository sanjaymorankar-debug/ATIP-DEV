"""
Strategy v2 (loss RCA, owner prompt 19 Nov 2026 06:48 PM IST): the entry filters and data-freshness gates of the
19 Nov review, behind ONE switch that is OFF unless config.json turns it on. With the block missing or
"enabled": false, every function here answers "off" and ATIP trades exactly as before (owner: "Enable them on Test
only. Do NOT switch them on for live/Prod order execution").

    atip_data/config.json
      "strategy_v2": {"enabled": true,                     # the master switch (default false)
                      "freshness_gate": true,              # each feature below defaults to on once enabled
                      "breadth_max_age_sessions": 1,
                      "max_signal_age_sessions": 1,
                      "retest_guard": true,
                      "nifty_gap_min_pct": null}             # e.g. -0.5 to try it; OFF by default (see below)

Features (report docs/reports/2026-11-19-loss-rca/LOSS_RCA_REPORT.md, section 7):
  freshness_gate            the breadth regime gate closes when its newest stored breadth is older than
                            breadth_max_age_sessions trading sessions before today (it used to read any age: a
                            stopped 21:20 job froze the gate open or shut); auto-pick takes no signal older than
                            max_signal_age_sessions trading sessions (it took up to 5 calendar days).
  retest_guard              a Kite-retest resting buy is not placed while the LTP is already at or under its planned
                            stop, and its fill is held while the session's regime gate is closed at fire time.
  (Not here: keeping stops alive during a manual STOP. The STOP also sets the kill switch the broker checks on every
   order, exits included -- changing that is the owner's call; see the report, LRCA-12.)
  nifty_gap_min_pct         no new auto-pick entry on a day Nifty 50's first print is this % or more under the
                            previous close (None = off). OFF by default: in the 19 Nov backtest it helped Jul-Sep and
                            hurt Oct-Nov (not robust), so it is a switch to try on Test, not a recommendation.
Every feature is read on each call (no restart needed), like orders/risk.py's limits.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path

log = logging.getLogger("atip.strategy_v2")

CONFIG_PATH = Path("atip_data") / "config.json"
KEY = "strategy_v2"
DEFAULTS = {"enabled": False, "freshness_gate": True, "breadth_max_age_sessions": 1, "max_signal_age_sessions": 1,
            "retest_guard": True, "nifty_gap_min_pct": None}
FEATURES = ("freshness_gate", "retest_guard")


def settings() -> dict:
    """The block merged over DEFAULTS; {"enabled": False, ...} when config.json has none or cannot be read."""
    out = dict(DEFAULTS)
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get(KEY)
    except Exception:                               # noqa: BLE001 -- no file / bad JSON: everything off
        raw = None
    if isinstance(raw, dict):
        out.update({k: v for k, v in raw.items() if not str(k).startswith("_")})
    out["enabled"] = out.get("enabled") is True
    return out


def enabled() -> bool:
    return settings()["enabled"]


def on(feature: str) -> bool:
    """True only when the master switch AND the feature are on."""
    s = settings()
    return bool(s["enabled"] and s.get(feature))


def value(name: str):
    """A numeric setting while the master switch is on, else None."""
    s = settings()
    return s.get(name) if s["enabled"] else None


def sessions_back(n: int, today: date | None = None) -> date:
    """The trading session n sessions before `today` (n=1: the previous session)."""
    from utils.trading_calendar import is_trading_day
    d = today or date.today()
    k = 0
    while k < max(int(n), 0):
        d -= timedelta(days=1)
        while not is_trading_day(d):
            d -= timedelta(days=1)
        k += 1
    return d


def stale(day, max_sessions, today: date | None = None) -> str | None:
    """Why `day` (the data's date) is too old for a decision made on `today`, or None."""
    if day is None:
        return "no date"
    d = date.fromisoformat(str(day)[:10])
    oldest = sessions_back(int(max_sessions), today)
    if d < oldest:
        return f"data of {d} is older than {oldest} ({int(max_sessions)} session(s) before {today or date.today()})"
    return None


def nifty_gap_block(conn, day: str) -> str | None:
    """With nifty_gap_min_pct set: why auto-pick should not enter today (Nifty's first print vs the previous close)."""
    lim = value("nifty_gap_min_pct")
    if lim is None:
        return None
    try:
        r = conn.execute("SELECT nifty50, nifty50_chg FROM index_levels WHERE date = ? AND time >= '09:15' "
                         "ORDER BY time LIMIT 1", (day,)).fetchone()
    except Exception:                               # noqa: BLE001
        return None
    if not r:
        return None
    r = dict(r) if hasattr(r, "keys") else {"nifty50": r[0], "nifty50_chg": r[1]}
    chg = r.get("nifty50_chg")
    if chg is None:
        return None
    if float(chg) <= float(lim):
        return f"strategy v2: Nifty opened {float(chg):+.2f}% (no new entry at or under {float(lim):+.2f}%)"
    return None


def describe() -> dict:
    s = settings()
    return {"enabled": s["enabled"], "features": {k: bool(s["enabled"] and s.get(k)) for k in FEATURES},
            "breadth_max_age_sessions": s["breadth_max_age_sessions"],
            "max_signal_age_sessions": s["max_signal_age_sessions"], "nifty_gap_min_pct": s["nifty_gap_min_pct"]}


if __name__ == "__main__":
    print(json.dumps(describe(), indent=2))
