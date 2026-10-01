"""
Macroeconomic data (W35: DP-14).

Free, keyless sources only:
    FRED      https://fred.stlouisfed.org/graph/fredgraph.csv?id=<code>   (St Louis Fed; includes OECD /
              IMF series for India)
    WORLDBANK https://api.worldbank.org/v2/country/IN/indicator/<code>?format=json   (annual)

SERIES is the catalogue (seeded into macro_series). Every observation is stored with a point-in-time
availability date:
    available_from = end of the observation period + the series' release_lag_days
so a backtest on day T only sees observations whose available_from <= T (point_in_time()). The lags
are conservative publication lags, not exact release dates -- a series is never visible earlier than
it really was, sometimes a few days later. A changed value for a stored period is a REVISION: the new
value replaces it (revised=1) and every fetch's full series is also kept in lake "macro_vintages"
(DP-22), so the as-first-published history is recoverable from the day this runs.

macro_calendar holds release / policy dates. ATIP has no free, reliable official calendar feed, so it
is owner-maintained (add_calendar / POST /api/macro/calendar) plus RULE-derived expected dates that are
labelled as such (source 'rule:...'), e.g. India CPI around the 12th of each month.

run_macro()  -> fetch every enabled series, store, emit MACRO events to market_event (QR-09) for new
               observations: direction UP / DOWN vs the previous observation (no consensus data exists,
               so this is change, not surprise).
"""

from __future__ import annotations

import calendar as _cal
import io
import json
import logging
from datetime import date, datetime, timedelta

import pandas as pd

log = logging.getLogger(__name__)

FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={code}"
WORLDBANK = "https://api.worldbank.org/v2/country/{country}/indicator/{code}?format=json&per_page=200"

# series_id: (name, country, source, code, frequency, unit, release_lag_days)
SERIES = {
    "IN_CPI_INDEX":   ("India CPI (all items, OECD)", "IN", "FRED", "INDCPIALLMINMEI", "M", "index", 20),
    "IN_IIP":         ("India industrial production (OECD)", "IN", "FRED", "INDPROINDMISMEI", "M", "index", 45),
    "IN_CALL_RATE":   ("India call money rate", "IN", "FRED", "IRSTCI01INM156N", "M", "%", 10),
    "IN_10Y_YIELD":   ("India 10-year government bond yield", "IN", "FRED", "INDIRLTLT01STM", "M", "%", 10),
    "IN_GDP_GROWTH":  ("India real GDP growth (annual)", "IN", "WORLDBANK", "NY.GDP.MKTP.KD.ZG", "A", "%", 200),
    "IN_INFLATION_A": ("India CPI inflation (annual)", "IN", "WORLDBANK", "FP.CPI.TOTL.ZG", "A", "%", 200),
    "USD_INR":        ("USD / INR (Fed H.10)", "IN", "FRED", "DEXINUS", "D", "INR", 2),
    "US_10Y":         ("US 10-year Treasury yield", "US", "FRED", "DGS10", "D", "%", 1),
    "US_FED_FUNDS":   ("US effective Fed funds rate", "US", "FRED", "FEDFUNDS", "M", "%", 2),
    "US_CPI":         ("US CPI (all urban)", "US", "FRED", "CPIAUCSL", "M", "index", 15),
    "BRENT":          ("Brent crude (EIA spot)", "GLOBAL", "FRED", "DCOILBRENTEU", "D", "USD/bbl", 3),
}
CPI_RULE_DAY = 12          # MOSPI publishes India CPI around the 12th of the following month


def _period_end(period: date, freq: str) -> date:
    if freq == "M":
        return period.replace(day=_cal.monthrange(period.year, period.month)[1])
    if freq == "Q":
        m = ((period.month - 1) // 3) * 3 + 3
        return date(period.year, m, _cal.monthrange(period.year, m)[1])
    if freq == "A":
        return date(period.year, 12, 31)
    return period


def seed(conn) -> int:
    n = 0
    for sid, (name, country, src, code, freq, unit, lag) in SERIES.items():
        n += conn.execute("INSERT OR IGNORE INTO macro_series (series_id,name,country,source,source_code,frequency,unit,"
                          "release_lag_days,status) VALUES (?,?,?,?,?,?,?,?, 'ACTIVE')",
                          (sid, name, country, src, code, freq, unit, lag)).rowcount or 0
    conn.commit()
    return n


def _http_get(url):
    import requests
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0 (ATIP research)"})
    r.raise_for_status()
    return r


def fetch_fred(code: str) -> list:
    df = pd.read_csv(io.StringIO(_http_get(FRED.format(code=code)).text))
    dcol = df.columns[0]
    vcol = code if code in df.columns else df.columns[-1]
    out = []
    for d, v in zip(df[dcol], pd.to_numeric(df[vcol], errors="coerce")):
        if pd.notna(v):
            out.append((date.fromisoformat(str(d)[:10]), float(v)))
    return out


def fetch_worldbank(code: str, country="IN") -> list:
    js = _http_get(WORLDBANK.format(country=country, code=code)).json()
    rows = js[1] if isinstance(js, list) and len(js) > 1 and js[1] else []
    return sorted((date(int(r["date"]), 1, 1), float(r["value"])) for r in rows
                  if r.get("value") is not None and str(r.get("date", "")).isdigit())


def store_series(conn, sid: str, obs: list) -> dict:
    row = conn.execute("SELECT frequency, release_lag_days FROM macro_series WHERE series_id=?", (sid,)).fetchone()
    freq, lag = row[0], int(row[1] or 0)
    new = revised = 0
    new_obs = []
    now = datetime.now()
    for period, value in obs:
        avail = _period_end(period, freq) + timedelta(days=lag)
        old = conn.execute("SELECT value FROM macro_observation WHERE series_id=? AND period=?",
                           (sid, str(period))).fetchone()
        if old is None:
            conn.execute("INSERT INTO macro_observation (series_id,period,value,available_from,first_seen) VALUES "
                         "(?,?,?,?,?)", (sid, str(period), value, str(avail), now))
            new += 1
            new_obs.append((period, value, avail))
        elif old[0] is None or abs(float(old[0]) - value) > 1e-9:
            conn.execute("UPDATE macro_observation SET value=?, revised=1 WHERE series_id=? AND period=?",
                         (value, sid, str(period)))
            revised += 1
    last = max((p for p, _ in obs), default=None)
    conn.execute("UPDATE macro_series SET last_fetch=?, last_period=?, status='ACTIVE', error=NULL WHERE series_id=?",
                 (now, str(last) if last else None, sid))
    conn.commit()
    return {"series_id": sid, "observations": len(obs), "new": new, "revised": revised, "new_obs": new_obs}


def _events(conn, sid, new_obs) -> int:
    """New observations -> market_event MACRO (QR-09), direction vs the previous period."""
    n = 0
    for period, value, avail in new_obs:
        prev = conn.execute("SELECT value FROM macro_observation WHERE series_id=? AND period<? ORDER BY period DESC "
                            "LIMIT 1", (sid, str(period))).fetchone()
        direction = None if not prev or prev[0] is None else ("UP" if value > prev[0] else "DOWN" if value < prev[0]
                                                               else "FLAT")
        try:
            n += conn.execute(
                "INSERT OR IGNORE INTO market_event (event_id,source,source_id,symbol,event_type,category,event_date,"
                "known_at,direction,value,payload_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"MC{sid}{period}", "macro", f"{sid}|{period}", None, "MACRO", "MACRO", str(avail), str(avail),
                 direction, value, json.dumps({"series_id": sid, "period": str(period),
                                               "previous": prev[0] if prev else None}), datetime.now())).rowcount or 0
        except Exception as e:
            log.debug(f"  macro event {sid} {period}: {e}")
    conn.commit()
    return n


def expected_calendar(conn, months_ahead=3) -> int:
    """Rule-derived expected India CPI release dates (labelled; the owner can correct them)."""
    n = 0
    d = date.today().replace(day=1)
    for _ in range(months_ahead + 1):
        rel = d.replace(day=CPI_RULE_DAY)
        n += conn.execute("INSERT OR IGNORE INTO macro_calendar (event_date,event,country,importance,series_id,source,note) "
                          "VALUES (?,?,?,?,?,?,?)", (str(rel), "India CPI (expected)", "IN", "HIGH", "IN_CPI_INDEX",
                                                     f"rule:day-{CPI_RULE_DAY}", "expected date by rule; verify on MOSPI")
                          ).rowcount or 0
        d = (d + timedelta(days=32)).replace(day=1)
    conn.commit()
    return n


def add_calendar(conn, event_date, event, importance="HIGH", country="IN", series_id=None, note="") -> dict:
    ev = str(event).strip()
    if not ev:
        raise ValueError("event is required")
    d = date.fromisoformat(str(event_date)[:10])
    conn.execute("INSERT OR REPLACE INTO macro_calendar (event_date,event,country,importance,series_id,source,note) "
                 "VALUES (?,?,?,?,?,?,?)", (str(d), ev, country, importance, series_id, "owner", note))
    conn.commit()
    return {"event_date": str(d), "event": ev}


def run_macro(conn=None) -> dict:
    from db.schema import get_connection, log_job
    own = conn is None
    conn = conn or get_connection()
    try:
        seed(conn)
        results, failed, events = [], [], 0
        for sid, src, code, country in conn.execute("SELECT series_id, source, source_code, country FROM macro_series "
                                                    "WHERE status<>'DISABLED'").fetchall():
            try:
                obs = fetch_fred(code) if src == "FRED" else fetch_worldbank(code, country if country != "GLOBAL" else "IN")
                r = store_series(conn, sid, obs)
                events += _events(conn, sid, r.pop("new_obs"))
                results.append(r)
                if obs:
                    from data import lake
                    lake.write("macro_vintages", date.today(), pd.DataFrame(
                        [{"series_id": sid, "period": str(p), "value": v} for p, v in obs]),
                        source=f"{src}:{code}", conn=conn)
            except Exception as e:
                failed.append(sid)
                conn.execute("UPDATE macro_series SET status='ERROR', error=?, last_fetch=? WHERE series_id=?",
                             (f"{type(e).__name__}: {e}"[:300], datetime.now(), sid))
                conn.commit()
                log.warning(f"  macro {sid}: {e}")
        expected_calendar(conn)
        status = "SUCCESS" if results and not failed else ("PARTIAL" if results else "FAILED")
        new = sum(r["new"] for r in results)
        log_job("macro_data", status, new)
        return {"status": status, "rows": new, "series": len(results), "failed": failed, "events": events}
    finally:
        if own:
            conn.close()


def point_in_time(conn, series_id: str, as_of=None, n: int | None = None) -> list:
    """Observations of a series that were available by as_of (default today), oldest first."""
    sql = "SELECT period, value, available_from, revised FROM macro_observation WHERE series_id=? AND available_from<=?"
    rows = conn.execute(sql + " ORDER BY period", (series_id, str(as_of or date.today())[:10])).fetchall()
    out = [dict(zip(("period", "value", "available_from", "revised"), r)) for r in rows]
    return out[-n:] if n else out


def latest_snapshot(conn, as_of=None) -> list:
    out = []
    for sid, name, unit, freq, status, err, lf in conn.execute(
            "SELECT series_id, name, unit, frequency, status, error, last_fetch FROM macro_series ORDER BY series_id"):
        pts = point_in_time(conn, sid, as_of, 2)
        cur = pts[-1] if pts else None
        prev = pts[-2] if len(pts) > 1 else None
        out.append({"series_id": sid, "name": name, "unit": unit, "frequency": freq, "status": status, "error": err,
                    "last_fetch": lf, "period": cur["period"] if cur else None, "value": cur["value"] if cur else None,
                    "previous": prev["value"] if prev else None, "available_from": cur["available_from"] if cur else None})
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    print(run_macro())
