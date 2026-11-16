"""
Seven years of NSE's official Capital Market Bhavcopy, kept locally (owner, 16 Nov 2026).

ATIP's 7-year price history (prices_daily) is Dhan's, adjusted for splits and bonuses as of the day it was fetched;
NSE's own end-of-day file -- the official record of what traded, at what price -- was kept only from 30 Jan 2025.
This keeps NSE's file for every session of the last seven years, so the stored history can be checked against it
and its gaps filled from it.

Store
    atip_data/raw/nse_archive/cm/<YYYY>/<file as published>                    the CM Bhavcopy zip, byte for byte
    atip_data/raw/nse_archive/delivery/<YYYY>/sec_bhavdata_full_<DDMMYYYY>.csv.gz  NSE's delivery file, gzipped
    lake "nse_cm_bhavcopy" (data/lake.py): one partition per session, every row of the file in one column set,
        raw as traded that day (no corporate-action adjustment), knowledge time 20:00 IST of the session:
        symbol series isin open high low close last prev_close volume turnover trades delivery_qty delivery_pct
    table nse_archive_session: one row per date asked -- session / holiday / pending / error -- so a holiday is
        not asked again, and NSE's session calendar for the years utils/trading_calendar.py does not list

Files (checked 16 Nov 2026): content/historical/EQUITIES/<YYYY>/<MON>/cm<DD><MON><YYYY>bhav.csv.zip up to
5 Jul 2024, content/cm/BhavCopy_NSE_CM_0_0_0_<YYYYMMDD>_F_0000.csv.zip (UDiFF) from 5 Jul 2024, and
products/content/sec_bhavdata_full_<DDMMYYYY>.csv (delivery) throughout. A holiday is a 404 in both formats.

Runs (pipeline/w39_jobs.py), newest session first, inside a time budget -- the scheduler runs one job at a time, so a
long download would hold up the evening re-score:
    weekdays 19:50 IST for 5 minutes (the day's session and some of the backlog); Saturday and Sunday 10:00 IST for 90
    minutes (the backlog: about 1,730 sessions, two files each, one request every 1.2 s); Sunday 12:00 IST verify().

verify(): per symbol, stored close / NSE close is one number between two corporate actions when the stored history
is right (NSE's file is raw, the stored history adjusted). A new ratio that holds for STEP_DAYS sessions is a
re-basing -- explained when corporate_actions or NSE's previous close shows an event that day (NSE's UDiFF file keeps
the unadjusted previous close on a bonus ex-date, CONCORDBIO 4 Nov 2026, so neither alone is enough); a day off its
level by more than the tolerance is a mismatch; an NSE row inside a symbol's stored history with no stored bar is
missing; a stored bar on a day NSE had no session is phantom. fill() writes the missing days from NSE on the stored basis (source 'bhavcopy_hist'), never
over a stored row, dry run unless --apply.

Config, atip_data/config.json:
    "bhavcopy_history": {"enabled": true, "years": 7, "time": "19:50", "minutes": 5, "weekend_time": "10:00",
                         "weekend_minutes": 90, "verify_time": "12:00", "pause_seconds": 1.2, "delivery": true}

CLI:
    python -m data.bhavcopy_history status
    python -m data.bhavcopy_history run [--minutes 80] [--max N] [--years 7]
    python -m data.bhavcopy_history verify [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--symbols A B] [--list]
    python -m data.bhavcopy_history fill [--symbols A B] [--apply]     # after `python -m ops backup --kind pre-release`
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import logging
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

DATASET = "nse_cm_bhavcopy"
RAW_DIR = Path("atip_data/raw/nse_archive")
REPORT_DIR = Path("atip_data/reports/price_check")
ARCHIVE = "https://nsearchives.nseindia.com"
OLD_URL = ARCHIVE + "/content/historical/EQUITIES/{y}/{mon}/cm{dd}{mon}{y}bhav.csv.zip"
UDIFF_URL = ARCHIVE + "/content/cm/BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip"
DELIVERY_URL = ARCHIVE + "/products/content/sec_bhavdata_full_{dmy}.csv"
MONTHS = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()   # not strftime("%b"): the OS locale
UDIFF_FROM = date(2024, 7, 5)       # both layouts exist for 5 Jul 2024; the old one ends there
PUBLISHED_BY = "17:00"              # NSE posts the day's CM file around 16:30 IST
PENDING_DAYS = 3                    # a 404 this recent may be a file NSE has not posted yet
MAX_ATTEMPTS = 5                    # failed tries of one date before the scheduled runs leave it (status lists it)
STOP_AFTER_ERRORS = 3               # errors in a row (403 / 5xx / timeouts): NSE is refusing -- stop, ask again later
KNOWN_AT = "20:00:00"               # knowledge time of a session's files: after NSE posts the delivery file
PRICE_SERIES = ("EQ", "BE", "BZ", "SM", "ST")     # series a prices_daily row can come from, most traded first
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0",
           "Accept": "*/*", "Referer": "https://www.nseindia.com/"}
DEFAULTS = {"enabled": True, "years": 7, "time": "19:50", "minutes": 5, "weekend_time": "10:00",
            "weekend_minutes": 90, "verify_time": "12:00", "pause_seconds": 1.2, "delivery": True}
COLUMNS = ["symbol", "series", "isin", "open", "high", "low", "close", "last", "prev_close", "volume", "turnover",
           "trades", "delivery_qty", "delivery_pct"]
OLD_MAP = {"SYMBOL": "symbol", "SERIES": "series", "ISIN": "isin", "OPEN": "open", "HIGH": "high", "LOW": "low",
           "CLOSE": "close", "LAST": "last", "PREVCLOSE": "prev_close", "TOTTRDQTY": "volume",
           "TOTTRDVAL": "turnover", "TOTALTRADES": "trades"}
UDIFF_MAP = {"TckrSymb": "symbol", "SctySrs": "series", "ISIN": "isin", "OpnPric": "open", "HghPric": "high",
             "LwPric": "low", "ClsPric": "close", "LastPric": "last", "PrvsClsgPric": "prev_close",
             "TtlTradgVol": "volume", "TtlTrfVal": "turnover", "TtlNbOfTxsExctd": "trades"}
NUMERIC = ["open", "high", "low", "close", "last", "prev_close", "volume", "turnover", "trades", "delivery_qty",
           "delivery_pct"]

DDL = """CREATE TABLE IF NOT EXISTS nse_archive_session (
    trade_date      TEXT PRIMARY KEY,
    status          TEXT NOT NULL,
    cm_format       TEXT,
    cm_url          TEXT,
    cm_rows         INTEGER,
    cm_sha256       TEXT,
    delivery_status TEXT,
    delivery_rows   INTEGER,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    fetched_at      TIMESTAMP,
    updated_at      TIMESTAMP
)"""
_FIELDS = ("status", "cm_format", "cm_url", "cm_rows", "cm_sha256", "delivery_status", "delivery_rows", "attempts",
           "last_error", "fetched_at")


def settings() -> dict:
    try:
        from ops.config import load
        raw = load().get("bhavcopy_history") or {}
    except Exception:
        raw = {}
    out = {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS}}
    for k in ("enabled", "delivery"):
        out[k] = out[k] is not False
    for k in ("years", "minutes", "weekend_minutes"):
        try:
            out[k] = max(1 if k == "years" else 0, int(out[k]))
        except (TypeError, ValueError):
            out[k] = DEFAULTS[k]
    try:
        out["pause_seconds"] = max(0.0, float(out["pause_seconds"]))
    except (TypeError, ValueError):
        out["pause_seconds"] = DEFAULTS["pause_seconds"]
    return out


def ensure_tables(conn):
    conn.execute(DDL)


def _d(v) -> date:
    return v if isinstance(v, date) and not isinstance(v, datetime) else date.fromisoformat(str(v)[:10])


def states(conn) -> dict:
    """date (YYYY-MM-DD) -> its nse_archive_session row (the table must exist: ensure_tables)."""
    cols = ("trade_date",) + _FIELDS
    return {str(r[0])[:10]: dict(zip(cols, r)) for r in conn.execute(f"SELECT {', '.join(cols)} FROM nse_archive_session")}


def calendar(conn) -> dict:
    """date (YYYY-MM-DD) -> 'session' / 'holiday' / 'pending' / 'error': NSE's own word, as far as archived."""
    try:
        return {d: s["status"] for d, s in states(conn).items()}
    except Exception:
        return {}


def _save(conn, d, **fields):
    cols = ["trade_date"] + list(fields) + ["updated_at"]
    vals = [str(d)] + list(fields.values()) + [datetime.now()]
    conn.execute(f"INSERT INTO nse_archive_session ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
                 f"ON CONFLICT(trade_date) DO UPDATE SET " + ", ".join(f"{c}=excluded.{c}" for c in cols[1:]), vals)
    conn.commit()


# ── fetching and parsing ──────────────────────────────────────────────────

class _Paced:
    """A requests session that waits `pause` seconds between calls: NSE's archive, politely."""

    def __init__(self, session, pause, sleep=time.sleep):
        self.session, self.pause, self.sleep, self.calls = session, pause, sleep, 0

    def get(self, url, **kw):
        if self.calls and self.pause:
            self.sleep(self.pause)
        self.calls += 1
        return self.session.get(url, headers=HEADERS, timeout=kw.get("timeout", 40))


def cm_urls(d: date) -> list:
    """(layout, url) for d, the layout NSE used that day first."""
    old = ("old", OLD_URL.format(y=d.year, mon=MONTHS[d.month - 1], dd=f"{d.day:02d}"))
    new = ("udiff", UDIFF_URL.format(ymd=d.strftime("%Y%m%d")))
    return [new, old] if d >= UDIFF_FROM else [old, new]


def _day(v):
    s = str(v or "").strip()
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        from data.bhavcopy import _nse_date
        return _nse_date(s)


def parse_cm(content: bytes, d: date) -> pd.DataFrame:
    """Every row of a CM Bhavcopy zip (either layout) in COLUMNS, delivery left empty. Raises if it is dated
    another day or its layout is unknown."""
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        raw = pd.read_csv(z.open(name), dtype=str)
    raw.columns = [str(c).strip() for c in raw.columns]
    if "TckrSymb" in raw.columns:
        cmap, day_col = UDIFF_MAP, "TradDt"
    elif "SYMBOL" in raw.columns:
        cmap, day_col = OLD_MAP, "TIMESTAMP"
    else:
        raise ValueError(f"unknown Bhavcopy layout: {list(raw.columns)[:6]}")
    days = {_day(v) for v in raw[day_col].dropna().unique()}
    if days != {d}:
        raise ValueError(f"the file is dated {sorted(str(x) for x in days)}, not {d}")
    df = raw.rename(columns=cmap)[list(cmap.values())].copy()
    for c in ("symbol", "series", "isin"):
        df[c] = df[c].astype(str).str.strip()
    df = df[df["symbol"].ne("") & df["symbol"].ne("nan")]
    df["delivery_qty"] = pd.NA
    df["delivery_pct"] = pd.NA
    for c in NUMERIC:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df[COLUMNS].reset_index(drop=True)


def parse_delivery_full(content: bytes, d: date) -> pd.DataFrame:
    """symbol, series, delivery_qty, delivery_pct of every row of NSE's sec_bhavdata_full dated d."""
    raw = pd.read_csv(io.BytesIO(content), dtype=str)
    raw.columns = [str(c).strip() for c in raw.columns]
    for c in raw.columns:
        raw[c] = raw[c].astype(str).str.strip()
    raw = raw[raw["DATE1"].map(_day) == d]
    return pd.DataFrame({"symbol": raw["SYMBOL"], "series": raw["SERIES"],
                         "delivery_qty": pd.to_numeric(raw["DELIV_QTY"], errors="coerce"),
                         "delivery_pct": pd.to_numeric(raw["DELIV_PER"], errors="coerce")}).drop_duplicates(
        ["symbol", "series"])


def _keep(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _stored_bars(conn, d) -> int:
    return conn.execute("SELECT COUNT(*) FROM prices_daily WHERE date=?", (str(d),)).fetchone()[0]


def _lake_frame(conn, d) -> pd.DataFrame:
    from data import lake
    df = lake.read(DATASET, d, d, conn=conn)
    return df.drop(columns=["_partition_date"], errors="ignore")


def archive_day(conn, d: date, session, delivery: bool = True, today: date | None = None) -> dict:
    """Fetch, keep and record one date. Returns {date, status, rows, delivery}."""
    from data import lake
    ensure_tables(conn)
    today = today or date.today()
    st = states(conn).get(str(d)) or {}
    recent = (today - d).days <= PENDING_DAYS
    attempts = int(st.get("attempts") or 0)
    now = datetime.now()
    df, url, fmt, sha, cm_new = None, st.get("cm_url"), st.get("cm_format"), st.get("cm_sha256"), False
    try:
        if st.get("status") != "session":
            got = None
            for layout, u in cm_urls(d):
                r = session.get(u)
                if r.status_code == 200 and r.content[:2] == b"PK":
                    got = (layout, u, r.content)
                    break
                if r.status_code != 404:
                    raise RuntimeError(f"HTTP {r.status_code} for {u}")
            if got is None:
                stored = _stored_bars(conn, d)
                status = "pending" if recent else ("error" if stored >= 50 else "holiday")
                err = (None if status == "holiday" else
                       "not posted yet" if recent else f"NSE has no file, but {stored} bars are stored for the day")
                _save(conn, d, status=status, attempts=attempts + (status != "holiday"), last_error=err,
                      fetched_at=now)
                return {"date": str(d), "status": status, "rows": 0, "delivery": None}
            fmt, url, content = got
            df = parse_cm(content, d)
            sha = hashlib.sha256(content).hexdigest()
            _keep(RAW_DIR / "cm" / str(d.year) / url.rsplit("/", 1)[1], content)
            cm_new = True
        deliv, dstatus, drows = None, st.get("delivery_status"), st.get("delivery_rows")
        if not delivery:
            dstatus = dstatus or "off"
        elif dstatus not in ("stored", "absent"):
            u = DELIVERY_URL.format(dmy=d.strftime("%d%m%Y"))
            r = session.get(u)
            if r.status_code == 200:
                deliv = parse_delivery_full(r.content, d)
                _keep(RAW_DIR / "delivery" / str(d.year) / (u.rsplit("/", 1)[1] + ".gz"), gzip.compress(r.content))
                dstatus, drows = "stored", int(len(deliv))
            elif r.status_code == 404:
                dstatus = "pending" if recent else "absent"
            else:
                dstatus = "error"
        if cm_new or deliv is not None:
            if df is None:
                df = _lake_frame(conn, d)
            if deliv is not None:
                df = df.drop(columns=["delivery_qty", "delivery_pct"]).merge(deliv, on=["symbol", "series"],
                                                                             how="left")
            lake.write(DATASET, d, df[COLUMNS], knowledge_time=f"{d} {KNOWN_AT}", source=url,
                       replace=not cm_new, conn=conn)
        _save(conn, d, status="session", cm_format=fmt, cm_url=url, cm_rows=int(len(df)) if df is not None
              else st.get("cm_rows"), cm_sha256=sha, delivery_status=dstatus, delivery_rows=drows, attempts=0,
              last_error=None, fetched_at=now)
        return {"date": str(d), "status": "session", "rows": int(len(df)) if cm_new else 0, "delivery": dstatus}
    except Exception as e:
        log.warning(f"  NSE archive {d}: {type(e).__name__}: {e}")
        if st.get("status") == "session":          # the CM file is kept; only the delivery step failed
            _save(conn, d, status="session", delivery_status="error", attempts=attempts + 1,
                  last_error=f"{type(e).__name__}: {e}"[:300], fetched_at=now)
        else:
            _save(conn, d, status="error", attempts=attempts + 1, last_error=f"{type(e).__name__}: {e}"[:300],
                  fetched_at=now)
        return {"date": str(d), "status": "error", "rows": 0, "delivery": None}


def candidate_dates(conn, start: date, end: date) -> list:
    """Every weekday in [start, end], and the weekend days NSE traded (a listed special session, or a day with at
    least 20 stored bars: Muhurat and budget days), newest first."""
    from utils.trading_calendar import NSE_SPECIAL_SESSIONS
    traded = {_d(r[0]) for r in conn.execute(
        "SELECT date FROM prices_daily WHERE date>=? AND date<=? GROUP BY date HAVING COUNT(*)>=20",
        (str(start), str(end)))}
    out, d = [], end
    while d >= start:
        if d.weekday() < 5 or d in NSE_SPECIAL_SESSIONS or d in traded:
            out.append(d)
        d -= timedelta(days=1)
    return out


def _due(st, delivery: bool) -> bool:
    if not st:
        return True
    if st["status"] == "holiday":
        return False
    if st["status"] == "session":
        return delivery and st.get("delivery_status") in ("pending", "error", "off", None) \
            and int(st.get("attempts") or 0) < MAX_ATTEMPTS
    return int(st.get("attempts") or 0) < MAX_ATTEMPTS


def run_archive(minutes=None, years=None, max_sessions=None, session=None, now=None, clock=time.monotonic,
                sleep=time.sleep) -> dict:
    """One budgeted pass, newest date first. Returns counts by outcome and what is left."""
    from ops.role import is_viewer
    if is_viewer():
        return {"status": "SKIPPED", "reason": "view-only server", "rows": 0}
    from db.schema import get_connection
    cfg = settings()
    minutes = cfg["minutes"] if minutes is None else minutes
    years = years or cfg["years"]
    now = now or datetime.now()
    today = now.date()
    end = today if now.strftime("%H:%M") >= PUBLISHED_BY else today - timedelta(days=1)
    target = today - timedelta(days=round(years * 365.25))
    deadline = clock() + minutes * 60 if minutes else None
    if session is None:
        import requests
        session = requests.Session()
    paced = _Paced(session, cfg["pause_seconds"], sleep)
    conn = get_connection()
    counts = {"session": 0, "holiday": 0, "pending": 0, "error": 0}
    rows = done = streak = 0
    stopped = None
    todo = []
    try:
        ensure_tables(conn)
        st = states(conn)
        todo = [d for d in candidate_dates(conn, target, end) if _due(st.get(str(d)), cfg["delivery"])]
        for d in todo:
            if (max_sessions and done >= max_sessions) or (deadline and clock() >= deadline):
                break
            r = archive_day(conn, d, paced, cfg["delivery"], today)
            counts[r["status"]] += 1
            rows += r["rows"]
            done += 1
            streak = streak + 1 if r["status"] == "error" else 0
            if streak >= STOP_AFTER_ERRORS:      # don't keep knocking: ATIP's other NSE calls share this address
                stopped = f"{streak} errors in a row, last {d}: stopped"
                log.warning(f"  NSE archive: {stopped}")
                break
    finally:
        conn.close()
    remaining = len(todo) - done
    status = "FAILED" if done and counts["error"] == done else "PARTIAL" if counts["error"] else "SUCCESS"
    log.info(f"  NSE archive: {counts['session']} sessions ({rows} rows), {counts['holiday']} holidays, "
             f"{counts['pending']} pending, {counts['error']} errors; {remaining} dates left back to {target}")
    return {"status": status, "rows": rows, "target": str(target), "dates": done, **counts, "remaining": remaining,
            "requests": paced.calls, "stopped": stopped}


def status(conn=None) -> dict:
    from db.schema import get_connection
    own = conn is None
    conn = conn or get_connection()
    try:
        ensure_tables(conn)
        cfg = settings()
        target = date.today() - timedelta(days=round(cfg["years"] * 365.25))
        st = states(conn)
        by = {}
        for s in st.values():
            by[s["status"]] = by.get(s["status"], 0) + 1
        sessions = sorted(d for d, s in st.items() if s["status"] == "session")
        deliv = {}
        for s in st.values():
            if s["status"] == "session":
                deliv[s["delivery_status"]] = deliv.get(s["delivery_status"], 0) + 1
        lake_rows = conn.execute("SELECT COALESCE(SUM(`rows`),0), COALESCE(SUM(bytes),0) FROM lake_partition "
                                 "WHERE dataset=?", (DATASET,)).fetchone()
        left = sum(1 for d in candidate_dates(conn, target, date.today()) if _due(st.get(str(d)), cfg["delivery"]))
        return {"target": str(target), "by_status": by, "delivery": deliv, "oldest_session": sessions[0] if sessions
                else None, "newest_session": sessions[-1] if sessions else None, "lake_rows": int(lake_rows[0]),
                "lake_bytes": int(lake_rows[1]), "dates_left": left,
                "errors": {d: s["last_error"] for d, s in st.items() if s["status"] == "error"}}
    finally:
        if own:
            conn.close()


# ── checking the stored history against NSE ───────────────────────────────

def _official(conn, start, end, symbols) -> pd.DataFrame:
    """symbol, date, open, high, low, close, prev_close, volume, delivery_qty, delivery_pct: one row per
    symbol-session (the most traded price series), from the lake."""
    from data import lake
    cols = ["symbol", "series", "open", "high", "low", "close", "prev_close", "volume", "delivery_qty",
            "delivery_pct"]
    rank = {s: i for i, s in enumerate(PRICE_SERIES)}
    frames = []
    for p in lake.parts(DATASET, start, end, conn=conn):
        try:
            df = (pd.read_parquet(p["path"], columns=cols) if p["format"] == "parquet"
                  else pd.read_csv(p["path"], compression="gzip", usecols=cols))
        except FileNotFoundError:
            log.warning(f"  {p['path']} is in the lake manifest but missing on disk")
            continue
        df = df[df["series"].isin(PRICE_SERIES) & df["symbol"].isin(symbols)]
        df = df.assign(_r=df["series"].map(rank)).sort_values("_r").drop_duplicates("symbol")
        frames.append(df.drop(columns=["_r", "series"]).assign(date=str(p["date"])[:10]))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=cols[:1] + ["date"] + cols[2:])


def covered_from(conn, end=None):
    """The oldest date from which every date up to the newest archived session is archived (a session or a
    holiday), or None. Inside that range two consecutive rows of a symbol are consecutive sessions for it, so NSE's
    previous close says exactly what changed between them; across an unarchived date it would not."""
    st = states(conn)
    done = sorted((d for d, s in st.items() if s["status"] == "session"), reverse=True)
    if not done:
        return None
    newest = _d(done[0]) if end is None else min(_d(done[0]), _d(end))
    first = None
    for d in candidate_dates(conn, newest - timedelta(days=4000), newest):
        if (st.get(str(d)) or {}).get("status") not in ("session", "holiday"):
            break
        first = d
    return first


def _universe(conn, symbols, min_bars=250) -> list:
    if symbols:
        return sorted({s.upper() for s in symbols})
    return [r[0] for r in conn.execute("SELECT symbol FROM prices_daily GROUP BY symbol HAVING COUNT(*)>=?",
                                       (min_bars,))]


STEP_DAYS = 3          # a new stored/NSE ratio that holds this many sessions is a re-basing, not a bad close


def _levels(ratio: list, tolerance: float, events: set, dates: list):
    """Split a symbol's stored/NSE close ratios (chronological) into levels. Returns (level per index, outlier
    indexes, steps). A value off the running level that the next STEP_DAYS values confirm starts a new level (a
    corporate action the stored history is adjusted for); one they do not is an outlier (a bad close). A level
    shorter than STEP_DAYS that does not start on a known event day is outliers too."""
    import statistics
    segs, out, i, n = [], [], 0, len(ratio)
    while i < n:
        v = ratio[i]
        if segs:
            lvl = statistics.median(ratio[j] for j in segs[-1] if j not in out)
            if abs(v / lvl - 1) <= tolerance:
                segs[-1].append(i)
                i += 1
                continue
            ahead = ratio[i + 1:i + 1 + STEP_DAYS]
            if not (ahead and all(abs(a / v - 1) <= tolerance for a in ahead)) and dates[i] not in events:
                segs[-1].append(i)
                out.append(i)
                i += 1
                continue
        segs.append([i])
        i += 1
    keep = []
    for seg in segs:                     # a short level not on an event day: its days are outliers
        if keep and len(seg) < STEP_DAYS and dates[seg[0]] not in events:
            out += seg
            keep[-1] += seg
        else:
            keep.append(seg)
    if len(keep) > 1 and len(keep[0]) < STEP_DAYS and dates[keep[1][0]] not in events:
        out += keep[0]                   # the series starts on a short level: those first days are the odd ones
        keep[1] = keep[0] + keep[1]
        keep = keep[1:]
    out = set(out)
    level, steps, prev = [None] * n, [], None
    for seg in keep:
        lvl = statistics.median(ratio[j] for j in seg if j not in out) if any(j not in out for j in seg) else None
        for j in seg:
            level[j] = lvl
        first = min(seg)
        if prev and lvl:
            steps.append({"date": dates[first], "factor": round(prev / lvl, 6), "explained": dates[first] in events})
        prev = lvl or prev
    return level, out, steps


def _events(conn, symbols) -> dict:
    """symbol -> ex-dates ATIP's corporate_actions table knows (any status): where the stored history may step."""
    try:
        rows = conn.execute("SELECT symbol, ex_date FROM corporate_actions").fetchall()
    except Exception:
        return {}
    out = {}
    for sym, ex in rows:
        if sym in symbols and ex:
            out.setdefault(sym, set()).add(str(ex)[:10])
    return out


def compare(conn, start=None, end=None, symbols=None, tolerance=0.005) -> dict:
    """symbol -> frame of every day in its stored span that NSE or prices_daily has: date, stored and NSE OHLCV,
    expected (the stored / NSE ratio of the day's level), deviation, flag ok / mismatch / missing / phantom /
    unmatched; frame.attrs["steps"]: where the stored history changes basis (factor, explained by a corporate action
    in corporate_actions or by NSE's adjusted previous close that day). NSE's UDiFF file keeps the unadjusted
    previous close on an ex-date (CONCORDBIO bonus, 4 Nov 2026), so the levels come from the ratios themselves."""
    syms = _universe(conn, symbols)
    ensure_tables(conn)
    lo_cov = covered_from(conn, end)
    if lo_cov is None:
        return {}
    start = max(_d(start), lo_cov) if start else lo_cov
    off = _official(conn, start, end, set(syms))
    if off.empty:
        return {}
    cal = calendar(conn)
    known = _events(conn, set(syms))
    sql = "SELECT symbol, date, open, high, low, close, volume FROM prices_daily WHERE symbol IN ({}) AND date>=? "
    args = [str(start)]
    if end:
        sql += "AND date<=? "
        args.append(str(end))
    stored, span = [], {}
    for i in range(0, len(syms), 400):
        chunk = syms[i:i + 400]
        marks = ",".join("?" * len(chunk))
        stored.append(pd.read_sql(sql.format(marks), conn, params=chunk + args))
        span.update({r[0]: (str(r[1])[:10], str(r[2])[:10]) for r in conn.execute(
            f"SELECT symbol, MIN(date), MAX(date) FROM prices_daily WHERE symbol IN ({marks}) GROUP BY symbol", chunk)})
    stored = pd.concat(stored, ignore_index=True)
    stored["date"] = stored["date"].astype(str).str[:10]
    by_sym = dict(tuple(stored.groupby("symbol")))
    out = {}
    for sym, o in off.groupby("symbol", sort=True):
        s = by_sym.get(sym)
        if s is None or s.empty:
            continue
        o = o.sort_values("date").reset_index(drop=True)
        nse_adj = (o["prev_close"] / o["close"].shift(1) - 1).abs() > tolerance      # NSE adjusted the day before
        events = known.get(sym, set()) | set(o.loc[nse_adj, "date"])
        m = s.merge(o, on="date", how="outer", suffixes=("_stored", "_nse")).sort_values("date")
        lo, hi = span[sym]               # the whole stored history: a bar missing on `start` is inside it too
        m = m[(m["date"] >= lo) & (m["date"] <= hi)].reset_index(drop=True)
        both = m["close_stored"].notna() & m["close_nse"].notna() & (m["close_nse"] > 0)
        idx = list(m.index[both])
        ratio = (m.loc[both, "close_stored"] / m.loc[both, "close_nse"]).tolist()
        level, odd, steps = _levels(ratio, tolerance, events, m.loc[both, "date"].tolist())
        lvl = pd.Series(float("nan"), index=m.index)
        lvl[idx] = level
        lvl = lvl.ffill().bfill()        # a missing day takes the level of the session before it
        m["expected"] = lvl
        m["deviation"] = (m["close_stored"] / m["close_nse"]) / lvl - 1
        flag = pd.Series("unmatched", index=m.index)
        flag[both] = "ok"
        flag[[idx[j] for j in odd]] = "mismatch"
        flag[m["close_stored"].isna() & m["close_nse"].notna()] = "missing"
        flag[m["close_stored"].notna() & m["close_nse"].isna() & m["date"].map(lambda x: cal.get(x) == "holiday")] \
            = "phantom"
        m["flag"] = flag
        m.attrs["steps"] = steps
        out[sym] = m
    return out


def verify(conn=None, start=None, end=None, symbols=None, tolerance=0.005, write_report=False) -> dict:
    """Every stored close against NSE's. Read-only; the report also goes to atip_data/reports/price_check/."""
    from db.schema import get_connection
    own = conn is None
    conn = conn or get_connection()
    try:
        ensure_tables(conn)
        lo_cov = covered_from(conn, end)
        frames = compare(conn, start, end, symbols, tolerance)
    finally:
        if own:
            conn.close()
    checked_from = max(_d(start), lo_cov) if start and lo_cov else lo_cov
    counts = {"ok": 0, "mismatch": 0, "missing": 0, "phantom": 0, "unmatched": 0}
    per = {}
    examples = []
    steps = []
    for sym, m in frames.items():
        steps += [{"symbol": sym, **st} for st in m.attrs.get("steps", [])]
        c = m["flag"].value_counts().to_dict()
        for k in counts:
            counts[k] += int(c.get(k, 0))
        bad = {k: int(c.get(k, 0)) for k in ("mismatch", "missing", "phantom") if c.get(k)}
        if bad:
            per[sym] = bad
        for r in m[m["flag"].isin(["mismatch", "missing", "phantom"])].itertuples():
            examples.append({"symbol": sym, "date": r.date, "flag": r.flag,
                             "stored_close": None if pd.isna(r.close_stored) else round(float(r.close_stored), 4),
                             "nse_close": None if pd.isna(r.close_nse) else round(float(r.close_nse), 4),
                             "expected_ratio": None if pd.isna(r.expected) else round(float(r.expected), 6),
                             "deviation_pct": None if pd.isna(r.deviation) else round(float(r.deviation) * 100, 3)})
    examples.sort(key=lambda e: (e["flag"] != "mismatch", -abs(e["deviation_pct"] or 0)))
    out = {"checked_at": datetime.now().isoformat(timespec="seconds"),
           "start": str(checked_from) if checked_from else None,
           "end": str(end) if end else None, "tolerance_pct": tolerance * 100, "symbols": len(frames),
           "days_compared": counts["ok"] + counts["mismatch"], **counts,
           "steps": len(steps), "unexplained_steps": [st for st in steps if not st["explained"]],
           "symbols_with_issues": len(per), "per_symbol": per, "issues": examples}
    if write_report:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = REPORT_DIR / f"{date.today()}.json"
        path.write_text(json.dumps(out, indent=1, default=str))
        out["report"] = str(path)
    return out


def fill(conn=None, symbols=None, apply=False, tolerance=0.005) -> dict:
    """The missing days (an NSE row inside a symbol's stored history, no stored bar) from NSE's file, on the stored
    basis: prices x the expected ratio, volume x the stored/NSE volume ratio of the nearest days on the same basis.
    Never over a stored row. Dry run unless apply."""
    from db.schema import get_connection
    own = conn is None
    conn = conn or get_connection()
    try:
        rows = []
        for sym, m in compare(conn, None, None, symbols, tolerance).items():
            miss = m[m["flag"] == "missing"]
            if miss.empty or m["expected"].isna().all():
                continue
            both = m[m["flag"] == "ok"]
            for r in miss.itertuples():
                same = both[(both["expected"] - r.expected).abs() < 1e-9]
                near = same.iloc[(pd.to_datetime(same["date"]) - pd.Timestamp(r.date)).abs().argsort()[:10]]
                vol = (near["volume_stored"] / near["volume_nse"]).replace([float("inf")], pd.NA).dropna()
                vf = float(vol.median()) if len(vol) else 1.0 / float(r.expected)
                px = lambda v: None if pd.isna(v) else round(float(v) * float(r.expected), 2)
                rows.append((sym, r.date, px(r.open_nse), px(r.high_nse), px(r.low_nse), px(r.close_nse),
                             None if pd.isna(r.volume_nse) else int(round(float(r.volume_nse) * vf)),
                             None if pd.isna(r.delivery_qty) else int(round(float(r.delivery_qty) * vf)),
                             None if pd.isna(r.delivery_pct) else float(r.delivery_pct)))
        written = 0
        if apply and rows:
            cur = conn.executemany(
                "INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,delivery_qty,delivery_pct,source) "
                "VALUES (?,?,?,?,?,?,?,?,?,'bhavcopy_hist') ON CONFLICT(symbol,date) DO NOTHING", rows)
            written = max(0, cur.rowcount or 0)
            conn.commit()
        return {"missing": len(rows), "written": written, "apply": apply,
                "rows": [dict(zip(("symbol", "date", "open", "high", "low", "close", "volume", "delivery_qty",
                                   "delivery_pct"), r)) for r in rows]}
    finally:
        if own:
            conn.close()


def purge(cutoff, conn=None, dry_run=True) -> dict:
    """Seven-year retention (db/purge.py HISTORY tier): lake partitions, kept files and session rows before cutoff."""
    from data import lake
    from db.schema import get_connection
    own = conn is None
    conn = conn or get_connection()
    try:
        ensure_tables(conn)
        cutoff = _d(cutoff)
        old = [p for p in RAW_DIR.glob("*/*/*") if p.is_file() and _file_day(p) and _file_day(p) < cutoff]
        out = {"lake_rows": lake.purge(DATASET, cutoff, conn=conn, dry_run=dry_run), "files": len(old),
               "sessions": conn.execute("SELECT COUNT(*) FROM nse_archive_session WHERE trade_date<?",
                                        (str(cutoff),)).fetchone()[0]}
        if not dry_run:
            for p in old:
                p.unlink()
            conn.execute("DELETE FROM nse_archive_session WHERE trade_date<?", (str(cutoff),))
            conn.commit()
        return out
    finally:
        if own:
            conn.close()


def _file_day(p: Path):
    """The session a kept file belongs to, from its name (cmDDMONYYYYbhav, ..._YYYYMMDD_F_..., _DDMMYYYY.csv)."""
    import re
    n = p.name
    m = re.search(r"cm(\d{2})([A-Z]{3})(\d{4})bhav", n)
    if m:
        from data.bhavcopy import _nse_date
        return _nse_date(f"{m[1]}-{m[2]}-{m[3]}")
    m = re.search(r"_(\d{8})_F_", n)
    if m:
        return datetime.strptime(m[1], "%Y%m%d").date()
    m = re.search(r"_(\d{8})\.csv", n)
    if m:
        return datetime.strptime(m[1], "%d%m%Y").date()
    return None


def run_scheduled(kind: str = "evening", now=None) -> dict:
    """The scheduled runs (pipeline/w39_jobs.py): "evening" every day for `minutes`, "weekend" on Saturday and Sunday
    for `weekend_minutes`, "verify" on Sunday (report to atip_data/reports/price_check/). A no-op on the view-only
    server and when switched off."""
    from ops.role import is_viewer
    cfg = settings()
    now = now or datetime.now()
    if not cfg["enabled"] or is_viewer():
        return {"status": "SKIPPED", "reason": "switched off" if not cfg["enabled"] else "view-only server", "rows": 0}
    if (kind == "weekend" and now.weekday() < 5) or (kind == "verify" and now.weekday() != 6):
        return {"status": "SKIPPED", "reason": "not today", "rows": 0}
    if kind == "verify":
        out = verify(write_report=True)
        return {"status": "SUCCESS", "rows": out["days_compared"],
                **{k: out[k] for k in ("start", "symbols", "mismatch", "missing", "phantom", "report") if k in out}}
    return run_archive(minutes=cfg["weekend_minutes"] if kind == "weekend" else cfg["minutes"], now=now)


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(description="Seven years of NSE's CM Bhavcopy, kept locally (16 Nov 2026)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    r = sub.add_parser("run")
    r.add_argument("--minutes", type=float)
    r.add_argument("--max", type=int)
    r.add_argument("--years", type=int)
    v = sub.add_parser("verify")
    v.add_argument("--start")
    v.add_argument("--end")
    v.add_argument("--symbols", nargs="+")
    v.add_argument("--tolerance", type=float, default=0.5, help="percent (default 0.5)")
    v.add_argument("--list", action="store_true", help="print every issue, not the first 25")
    f = sub.add_parser("fill")
    f.add_argument("--symbols", nargs="+")
    f.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        print(json.dumps(status(), indent=1, default=str))
    elif a.cmd == "run":
        out = run_archive(minutes=a.minutes if a.minutes is not None else None, years=a.years, max_sessions=a.max)
        print(out)
    elif a.cmd == "verify":
        out = verify(start=a.start, end=a.end, symbols=a.symbols, tolerance=a.tolerance / 100, write_report=True)
        issues = out.pop("issues")
        out.pop("per_symbol")
        print(json.dumps(out, indent=1, default=str))
        for e in issues if a.list else issues[:25]:
            print(f"  {e['flag']:<8} {e['symbol']:<12} {e['date']}  stored {e['stored_close']}  NSE {e['nse_close']}"
                  f"  expected ratio {e['expected_ratio']}  off {e['deviation_pct']}%")
    else:
        out = fill(symbols=a.symbols, apply=a.apply)
        print(f"{out['missing']} missing day(s); " + (f"wrote {out['written']}" if a.apply else "dry run, nothing written"))
        for r in out["rows"][:25]:
            print(f"  {r['symbol']:<12} {r['date']}  close {r['close']}  volume {r['volume']}")


if __name__ == "__main__":
    main()
