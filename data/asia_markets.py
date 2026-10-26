"""
Asia markets (Task 3, 2026-10-26): South Korea, Taiwan, Pakistan and Japan index signals.

Six indices -- Nikkei 225, TOPIX (JPX), KOSPI, KOSDAQ (KRX), TAIEX (TWSE), KSE-100 (PSX) -- each with a
primary data source and at least one fallback. Exchange hours, holiday calendars, sources, weights and
thresholds are configuration (data/asia_exchanges.json, overridable from atip_data/config.json
"asia_markets"), not code.

Sources were VERIFIED from the owner's Mac on 2026-10-26 before being relied on (the lesson of Yahoo's
dead ^CNX sector tickers, data/markets.py). verify_sources() re-runs the probe and writes the result to
asia_source_health; every fetch also records how each source answered, so a source that stops returning
data is flagged on /api/asia/sources and in the log, and the fetch moves to the next source.

    index   primary                         fallback(s)                      2026-10-26 probe
    N225    Naver world .N225               Yahoo ^N225                      both OK (Yahoo's newest bar had
                                                                             a NaN close, so it lags a day)
    TOPIX   Naver world .TOPX               Yahoo ^TOPX                      Yahoo EMPTY -> flagged
    KOSPI   Naver Korea KOSPI               Yahoo ^KS11                      both OK
    KOSDAQ  Naver Korea KOSDAQ              Yahoo ^KQ11                      both OK
    TAIEX   TWSE official MI_5MINS_HIST     Yahoo ^TWII, Naver .TWII         all OK
    KSE100  Investing.com historical 49677  PSX DPS eod, Yahoo ^KSE          PSX HTTP 462, Yahoo EMPTY -> flagged
    Stooq (every index) answers with a JavaScript bot wall, so it is not configured at all.

Tables (all date-keyed, 600-day LONG retention in db/purge.py):
    global_indices_daily  (index_code, date) daily OHLC, previous close, % change, session status, source
    asia_signals          (index_code, date) BULLISH / NEUTRAL / BEARISH, confidence, trend / momentum /
                          volatility / gap components
    asia_sentiment        (date, run_label)  the combined Asia Sentiment Score (0-100, 50 = neutral) with
                          the Pakistan geopolitical risk flag as a separate factor
    asia_source_health    (index_code, kind, ticker) last probe / fetch result per source

Signals are INFORMATIONAL ONLY: nothing here places, changes or gates an order. The Asia Sentiment Score
feeds Market Health through weight_config ("MH", "Asia") and the pre-market report.

CLI:
    python -m data.asia_markets verify          probe every source, print and store the result
    python -m data.asia_markets run [--label X] fetch, store, signal, sentiment
    python -m data.asia_markets status
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import math
import re
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
CONFIG_FILE = Path(__file__).with_name("asia_exchanges.json")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124 Safari/537.36", "Accept-Language": "en-US,en;q=0.9"}
TIMEOUT = 20
RETRY_PAUSE = 2.0
TREND_DEADBAND = 0.002          # 0.2%: closer than this is "at", not above or below

DDL = [
    """CREATE TABLE IF NOT EXISTS global_indices_daily (
        index_code TEXT NOT NULL, date TEXT NOT NULL, open REAL, high REAL, low REAL, close REAL,
        volume REAL, prev_close REAL, chg_pct REAL, session_status TEXT, source TEXT, is_fallback INTEGER DEFAULT 0,
        fetched_at TIMESTAMP, PRIMARY KEY (index_code, date))""",
    """CREATE TABLE IF NOT EXISTS asia_signals (
        index_code TEXT NOT NULL, date TEXT NOT NULL, `signal` TEXT, confidence REAL, score REAL,
        trend REAL, momentum REAL, volatility REAL, gap_pct REAL, gap REAL, vol_regime TEXT, bars INTEGER,
        components_json TEXT, computed_at TIMESTAMP, PRIMARY KEY (index_code, date))""",
    """CREATE TABLE IF NOT EXISTS asia_sentiment (
        date TEXT NOT NULL, run_label TEXT NOT NULL, score REAL, `signal` TEXT, confidence REAL, coverage REAL,
        pk_risk_level TEXT, pk_risk_json TEXT, components_json TEXT, weights_json TEXT, computed_at TIMESTAMP,
        PRIMARY KEY (date, run_label))""",
    """CREATE TABLE IF NOT EXISTS asia_source_health (
        index_code TEXT NOT NULL, kind TEXT NOT NULL, ticker TEXT NOT NULL, ok INTEGER, n_rows INTEGER,
        last_date TEXT, error TEXT, checked_at TIMESTAMP, via TEXT, PRIMARY KEY (index_code, kind, ticker))""",
]


# ── configuration ──────────────────────────────────────────────────────────
def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def settings() -> dict:
    """data/asia_exchanges.json, deep-merged with atip_data/config.json "asia_markets"."""
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    try:
        user = json.loads(Path("atip_data/config.json").read_text(encoding="utf-8")).get("asia_markets") or {}
    except Exception:
        user = {}
    return _merge(cfg, user)


def ensure_tables(conn):
    for ddl in DDL:
        conn.execute(ddl)
    ensure_mh_weight(conn)


def ensure_mh_weight(conn, cfg=None):
    """The Asia Sentiment Score's place in Market Health: weight_config ("MH", "Asia"), the same table every
    other MH weight lives in. asia_markets.mh_weight in config.json sets it; 0 switches the input off."""
    w = float((cfg or settings()).get("mh_weight") or 0)
    try:
        cur = conn.execute("SELECT weight, active FROM weight_config WHERE index_name='MH' AND variable='Asia'").fetchone()
    except Exception:
        return
    if cur is None:
        conn.execute("INSERT OR IGNORE INTO weight_config(index_name,variable,weight,description,regime) "
                     "VALUES('MH','Asia',?,'Asia Sentiment Score (Task 3)','ALL')", (w,))
    elif abs(float(cur[0] or 0) - w) > 1e-9:
        conn.execute("UPDATE weight_config SET weight=? WHERE index_name='MH' AND variable='Asia'", (w,))
    if w <= 0:
        conn.execute("UPDATE weight_config SET active=0 WHERE index_name='MH' AND variable='Asia'")
    conn.commit()


# ── exchange sessions ──────────────────────────────────────────────────────
def _hm(s) -> dtime:
    h, m = (int(x) for x in str(s).split(":"))
    return dtime(h, m)


def exchange_sessions(ex: dict, d: date) -> list:
    special = (ex.get("special_sessions") or {}).get(str(d))
    if special:
        return special
    return (ex.get("sessions_by_weekday") or {}).get(str(d.weekday())) or ex.get("sessions") or []


def is_exchange_day(ex: dict, d: date) -> bool:
    return d.weekday() in (ex.get("weekdays") or [0, 1, 2, 3, 4]) and str(d) not in set(ex.get("holidays") or [])


def session_status(exchange: str, now: datetime | None = None, cfg: dict | None = None) -> str:
    """OPEN / BREAK / PRE_OPEN / CLOSED / HOLIDAY / WEEKEND for an exchange at `now` (any timezone)."""
    cfg = cfg or settings()
    ex = cfg["exchanges"][exchange]
    local = (now or datetime.now(IST)).astimezone(ZoneInfo(ex["tz"]))
    d, t = local.date(), local.time()
    if d.weekday() not in (ex.get("weekdays") or [0, 1, 2, 3, 4]):
        return "WEEKEND"
    if str(d) in set(ex.get("holidays") or []):
        return "HOLIDAY"
    sess = [(_hm(a), _hm(b)) for a, b in exchange_sessions(ex, d)]
    if not sess:
        return "CLOSED"
    for a, b in sess:
        if a <= t < b:
            return "OPEN"
    if t < sess[0][0]:
        return "PRE_OPEN"
    if t >= sess[-1][1]:
        return "CLOSED"
    return "BREAK"


def session_times_ist(exchange: str, d: date | None = None, cfg: dict | None = None) -> list:
    """The exchange's sessions on its local date d, converted to IST "HH:MM" pairs (for the dashboard)."""
    cfg = cfg or settings()
    ex = cfg["exchanges"][exchange]
    tz = ZoneInfo(ex["tz"])
    d = d or datetime.now(tz).date()
    out = []
    for a, b in exchange_sessions(ex, d):
        sa = datetime.combine(d, _hm(a), tz).astimezone(IST)
        sb = datetime.combine(d, _hm(b), tz).astimezone(IST)
        out.append([sa.strftime("%H:%M"), sb.strftime("%H:%M")])
    return out


def local_date(exchange: str, now: datetime | None = None, cfg: dict | None = None) -> date:
    cfg = cfg or settings()
    return (now or datetime.now(IST)).astimezone(ZoneInfo(cfg["exchanges"][exchange]["tz"])).date()


# ── source fetchers: each returns ascending [{"date","open","high","low","close","volume"}] ──
def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        f = float(v)
    else:
        s = str(v).replace(",", "").strip()
        mult = 1.0
        if s[-1:] in "KMB" and s[:-1]:
            mult = {"K": 1e3, "M": 1e6, "B": 1e9}[s[-1]]
            s = s[:-1]
        try:
            f = float(s) * mult
        except ValueError:
            return None
    return None if (f != f or math.isinf(f)) else f


def _get(url, headers=None):
    import requests
    r = requests.get(url, headers={**UA, **(headers or {})}, timeout=TIMEOUT)
    if r.status_code >= 400:
        raise ConnectionError(f"HTTP {r.status_code}")
    return r


def fetch_yahoo(ticker: str, days: int) -> list:
    from data.markets import _yf_download
    df = _yf_download(ticker, period=f"{max(days + days // 2, 10)}d", interval="1d", progress=False,
                      auto_adjust=False)
    cols = {}
    for name in ("Open", "High", "Low", "Close", "Volume"):
        s = df[name]
        if hasattr(s, "columns"):                    # yfinance >= 0.2.5x: one column per ticker
            s = s.iloc[:, 0]
        cols[name] = s
    out = []
    for idx in cols["Close"].index:
        c = _num(cols["Close"].get(idx))
        if c is None:
            continue                                  # an unfinished bar comes back with a NaN close
        out.append({"date": str(idx.date()), "open": _num(cols["Open"].get(idx)), "high": _num(cols["High"].get(idx)),
                    "low": _num(cols["Low"].get(idx)), "close": c, "volume": _num(cols["Volume"].get(idx))})
    return out[-days:]


def fetch_naver_world(ticker: str, days: int) -> list:
    """api.stock.naver.com world index prices (Nikkei .N225, TOPIX .TOPX, TAIEX .TWII), newest first."""
    out, page = [], 1
    while len(out) < days and page <= 10:
        rows = _get(f"https://api.stock.naver.com/index/{ticker}/price?page={page}&pageSize=60").json()
        if not isinstance(rows, list) or not rows:
            break
        for r in rows:
            d = str(r.get("localTradedAt") or "")[:10]
            c = _num(r.get("closePrice"))
            if len(d) == 10 and c:
                out.append({"date": d, "open": _num(r.get("openPrice")), "high": _num(r.get("highPrice")),
                            "low": _num(r.get("lowPrice")), "close": c, "volume": None})
        page += 1
    return sorted({r["date"]: r for r in out}.values(), key=lambda r: r["date"])[-days:]


def fetch_naver_kr(ticker: str, days: int) -> list:
    """fchart.stock.naver.com daily chart (KOSPI, KOSDAQ): <item data="YYYYMMDD|o|h|l|c|v"/>."""
    text = _get(f"https://fchart.stock.naver.com/sise.nhn?symbol={ticker}&timeframe=day&count={days}"
                f"&requestType=0").text
    out = []
    for m in re.finditer(r'data="(\d{8})\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|([^"]*)"', text):
        d = m.group(1)
        c = _num(m.group(5))
        if c:
            out.append({"date": f"{d[:4]}-{d[4:6]}-{d[6:]}", "open": _num(m.group(2)), "high": _num(m.group(3)),
                        "low": _num(m.group(4)), "close": c, "volume": _num(m.group(6))})
    return out[-days:]


def fetch_twse(ticker: str, days: int, today: date | None = None) -> list:
    """TWSE's own TAIEX OHLC (MI_5MINS_HIST), one month per request, walking back until `days` bars."""
    today = today or datetime.now(IST).date()
    out, m, tries = [], today.replace(day=1), 0
    while len(out) < days and tries < max(2, days // 18 + 2):
        js = _get(f"https://www.twse.com.tw/rwd/en/TAIEX/MI_5MINS_HIST?date={m:%Y%m%d}&response=json").json()
        if js.get("stat") == "OK":
            for row in js.get("data") or []:
                y, mo, dd = (int(x) for x in str(row[0]).split("/"))
                y = y + 1911 if y < 1911 else y           # the Chinese-language page uses ROC years
                c = _num(row[4])
                if c:
                    out.append({"date": f"{y:04d}-{mo:02d}-{dd:02d}", "open": _num(row[1]), "high": _num(row[2]),
                                "low": _num(row[3]), "close": c, "volume": None})
        m = (m - timedelta(days=1)).replace(day=1)
        tries += 1
    return sorted({r["date"]: r for r in out}.values(), key=lambda r: r["date"])[-days:]


def fetch_investing(ticker: str, days: int, today: date | None = None) -> list:
    """api.investing.com daily history for an instrument id (KSE-100 = 49677)."""
    today = today or datetime.now(IST).date()
    start = today - timedelta(days=int(days * 1.6) + 10)
    js = _get(f"https://api.investing.com/api/financialdata/historical/{ticker}?start-date={start}&end-date={today}"
              f"&time-frame=Daily&add-missing-rows=false", {"domain-id": "www"}).json()
    out = []
    for r in js.get("data") or []:
        c = _num(r.get("last_closeRaw") or r.get("last_close"))
        d = str(r.get("rowDateTimestamp") or "")[:10]
        if c and len(d) == 10:
            out.append({"date": d, "open": _num(r.get("last_openRaw")), "high": _num(r.get("last_maxRaw")),
                        "low": _num(r.get("last_minRaw")), "close": c, "volume": _num(r.get("volumeRaw"))})
    return sorted(out, key=lambda r: r["date"])[-days:]


def fetch_psx_dps(ticker: str, days: int) -> list:
    """PSX Data Portal EOD series: {"data": [[epoch, close, volume, open], ...]} (HTTP 462 from India on
    2026-10-26 -- kept as a fallback, its health is recorded)."""
    js = _get(f"https://dps.psx.com.pk/timeseries/eod/{ticker}",
              {"Referer": "https://dps.psx.com.pk/", "X-Requested-With": "XMLHttpRequest"}).json()
    out = []
    for r in js.get("data") or []:
        try:
            d = datetime.fromtimestamp(int(r[0]), ZoneInfo("Asia/Karachi")).date()
        except Exception:
            continue
        c = _num(r[1])
        if c:
            out.append({"date": str(d), "open": _num(r[3]) if len(r) > 3 else None, "high": None, "low": None,
                        "close": c, "volume": _num(r[2]) if len(r) > 2 else None})
    return sorted(out, key=lambda r: r["date"])[-days:]


FETCHERS = {"yahoo": fetch_yahoo, "naver_world": fetch_naver_world, "naver_kr": fetch_naver_kr,
            "twse": fetch_twse, "investing": fetch_investing, "psx_dps": fetch_psx_dps}


def validate_bars(bars: list, stale_after_days: int, today: date) -> str | None:
    """None when the bars are usable, else why not. A source that answers with an old or broken series is
    as useless as one that does not answer (the Yahoo ^CNX tickers returned 0.00% for every index)."""
    if not bars:
        return "no data returned"
    last = bars[-1]
    try:
        age = (today - date.fromisoformat(last["date"])).days
    except ValueError:
        return f"unparseable date {last['date']!r}"
    if age > stale_after_days:
        return f"stale: last bar {last['date']} is {age} days old"
    for b in bars[-5:]:
        if not b.get("close") or b["close"] <= 0:
            return f"non-positive close on {b['date']}"
        if b.get("high") and b.get("low") and b["high"] < b["low"]:
            return f"high < low on {b['date']}"
    closes = [b["close"] for b in bars[-6:]]
    if len(closes) >= 6 and len(set(closes)) == 1:
        return "flat series (every recent close identical)"
    return None


def _record_health(conn, code, src, ok, rows, last_date, error, via):
    conn.execute("INSERT INTO asia_source_health (index_code,kind,ticker,ok,n_rows,last_date,error,checked_at,via) "
                 "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(index_code,kind,ticker) DO UPDATE SET ok=excluded.ok, "
                 "n_rows=excluded.n_rows, last_date=excluded.last_date, error=excluded.error, "
                 "checked_at=excluded.checked_at, via=excluded.via",
                 (code, src["kind"], src["ticker"], int(bool(ok)), rows, last_date, error,
                  datetime.now(IST).replace(tzinfo=None), via))


def fetch_index(conn, code: str, days: int | None = None, cfg: dict | None = None, today: date | None = None) -> dict:
    """Bars for one index from its first source that returns valid data; every attempt is recorded in
    asia_source_health and a fallback is logged as one."""
    cfg = cfg or settings()
    spec = cfg["indexes"][code]
    days = days or int(cfg.get("history_days") or 260)
    today = today or local_date(spec["exchange"], cfg=cfg)
    errors = []
    for i, src in enumerate(spec["sources"]):
        fn = FETCHERS.get(src["kind"])
        if fn is None:
            errors.append(f"{src['kind']}: unknown source kind")
            continue
        bars, why = [], None
        for attempt in range(2):                    # one retry: a proxy hiccup is not a dead source
            try:
                bars = fn(src["ticker"], days)
                why = validate_bars(bars, int(cfg.get("stale_after_days") or 7), today)
            except Exception as e:
                bars, why = [], f"{type(e).__name__}: {str(e)[:160]}"
            if why is None or not why.startswith(("ProxyError", "ConnectionError", "ReadTimeout", "ConnectTimeout")):
                break
            time.sleep(RETRY_PAUSE)
        _record_health(conn, code, src, why is None, len(bars), bars[-1]["date"] if bars else None, why, "fetch")
        if why is None:
            if i:
                log.warning(f"  ⚠ {code}: primary source failed ({'; '.join(errors)}) — using fallback "
                            f"{src['kind']}:{src['ticker']}")
            return {"code": code, "bars": bars, "source": f"{src['kind']}:{src['ticker']}", "fallback": bool(i),
                    "errors": errors}
        errors.append(f"{src['kind']}:{src['ticker']}: {why}")
        log.warning(f"  ✗ {code} source {src['kind']}:{src['ticker']} — {why}")
    log.error(f"  ✗ {code}: every source failed — {'; '.join(errors)}")
    return {"code": code, "bars": [], "source": None, "fallback": False, "errors": errors}


def verify_sources(cfg: dict | None = None, conn=None) -> list:
    """Probe EVERY configured source of every index (not just until one works) and store the result."""
    from db.schema import get_connection
    cfg = cfg or settings()
    own = conn is None
    conn = conn or get_connection()
    out = []
    try:
        ensure_tables(conn)
        for code, spec in cfg["indexes"].items():
            today = local_date(spec["exchange"], cfg=cfg)
            for src in spec["sources"]:
                try:
                    bars = FETCHERS[src["kind"]](src["ticker"], 30)
                    why = validate_bars(bars, int(cfg.get("stale_after_days") or 7), today)
                except Exception as e:
                    bars, why = [], f"{type(e).__name__}: {str(e)[:160]}"
                _record_health(conn, code, src, why is None, len(bars), bars[-1]["date"] if bars else None, why, "verify")
                (log.info if why is None else log.warning)(
                    f"  {'✓' if why is None else '✗ FLAGGED'} {code:<7} {src['kind']}:{src['ticker']}"
                    + (f" — {len(bars)} bars to {bars[-1]['date']}" if why is None else f" — {why}"))
                out.append({"index": code, "kind": src["kind"], "ticker": src["ticker"], "ok": why is None,
                            "rows": len(bars), "last_date": bars[-1]["date"] if bars else None, "error": why})
        conn.commit()
    finally:
        if own:
            conn.close()
    return out


# ── storage ────────────────────────────────────────────────────────────────
def store_bars(conn, code: str, fetched: dict, cfg: dict, now: datetime | None = None) -> int:
    spec = cfg["indexes"][code]
    bars = fetched["bars"]
    if not bars:
        return 0
    now = now or datetime.now(IST)
    today_local = local_date(spec["exchange"], now, cfg)
    status_now = session_status(spec["exchange"], now, cfg)
    rows, prev = [], None
    for b in bars:
        chg = round((b["close"] - prev) / prev * 100, 3) if prev else None
        st = status_now if b["date"] == str(today_local) else "CLOSED"
        rows.append((code, b["date"], b.get("open"), b.get("high"), b.get("low"), b["close"], b.get("volume"),
                     prev, chg, st, fetched["source"], int(fetched["fallback"]), now.replace(tzinfo=None)))
        prev = b["close"]
    rows = rows[1:] if len(rows) > 1 else rows          # the first bar has no previous close in the window
    conn.executemany(
        "INSERT INTO global_indices_daily (index_code,date,open,high,low,close,volume,prev_close,chg_pct,"
        "session_status,source,is_fallback,fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(index_code,date) DO UPDATE SET open=excluded.open, high=excluded.high, low=excluded.low, "
        "close=excluded.close, volume=COALESCE(excluded.volume, global_indices_daily.volume), "
        "prev_close=COALESCE(excluded.prev_close, global_indices_daily.prev_close), "
        "chg_pct=COALESCE(excluded.chg_pct, global_indices_daily.chg_pct), session_status=excluded.session_status, "
        "source=excluded.source, is_fallback=excluded.is_fallback, fetched_at=excluded.fetched_at", rows)
    return len(rows)


def history(conn, code: str, upto: str | None = None, n: int = 260) -> list:
    q = "SELECT date, open, high, low, close FROM global_indices_daily WHERE index_code=?"
    args = [code]
    if upto:
        q += " AND date<=?"
        args.append(upto)
    rows = conn.execute(q + " ORDER BY date DESC LIMIT ?", (*args, n)).fetchall()
    return [dict(r) if hasattr(r, "keys") else dict(zip(("date", "open", "high", "low", "close"), r))
            for r in rows][::-1]


# ── per-market signal ──────────────────────────────────────────────────────
def _ema(xs, n):
    if len(xs) < n:
        return None
    k = 2 / (n + 1)
    e = sum(xs[:n]) / n
    for x in xs[n:]:
        e = x * k + e * (1 - k)
    return e


def _rsi(xs, n=14):
    if len(xs) < n + 1:
        return None
    gains = losses = 0.0
    for a, b in zip(xs[-n - 1:-1], xs[-n:]):
        d = b - a
        gains += max(d, 0)
        losses += max(-d, 0)
    if losses == 0:
        return 100.0
    rs = (gains / n) / (losses / n)
    return 100 - 100 / (1 + rs)


def _clip(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


def _stdev(xs):
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def compute_signal(bars: list, cfg: dict) -> dict | None:
    """
    Trend, momentum, volatility and gap from previous close -> one score in [-100, 100], a BULLISH / NEUTRAL /
    BEARISH label and a 0-100 confidence. Needs 15 bars; with fewer than 50 the trend uses EMA-20 only and
    the confidence is cut for completeness.

      trend       close vs EMA-20 (+-0.4), EMA-20 vs EMA-50 (+-0.4), 10-session EMA-20 slope (+-0.2)
      momentum    RSI-14 mapped (RSI-50)/25 and the 5-session return at 3% full scale, averaged
      volatility  20-session realised vol vs the longer-window median: a vol spike (x1.3+) reads risk-off
                  down to -1, a quiet tape (x0.8-) +0.3
      gap         the latest session's open vs the previous close, 1.5% full scale (the 06:00 IST run sees
                  Tokyo's and Seoul's opening gap -- they open at 05:30 IST)
    """
    closes = [b["close"] for b in bars if b.get("close")]
    if len(closes) < 15:
        return None
    w = cfg.get("signal_weights") or {}
    th = cfg.get("thresholds") or {}
    last = closes[-1]
    e20, e50 = _ema(closes, 20), _ema(closes, 50)
    def side(a, b, band=TREND_DEADBAND):
        """+1 / -1 when a is above / below b by more than the band, else 0: a flat, choppy tape must not
        score a full trend from rounding (defect found in testing 2026-10-26: a sideways series read BULLISH)."""
        if not a or not b:
            return 0
        r = a / b - 1
        return 1 if r > band else -1 if r < -band else 0
    trend = 0.0
    if e20:
        trend += 0.4 * side(last, e20)
        if len(closes) >= 30:
            trend += 0.2 * side(e20, _ema(closes[:-10], 20), TREND_DEADBAND / 2)
    if e50:
        trend += 0.4 * side(e20, e50)
    rsi = _rsi(closes)
    roc5 = (last / closes[-6] - 1) * 100 if len(closes) >= 6 else None
    mparts = [p for p in ((_clip((rsi - 50) / 25) if rsi is not None else None),
                          (_clip(roc5 / 3) if roc5 is not None else None)) if p is not None]
    momentum = sum(mparts) / len(mparts) if mparts else 0.0
    rets = [b / a - 1 for a, b in zip(closes[:-1], closes[1:])]
    vol20 = _stdev(rets[-20:])
    vols = [_stdev(rets[i - 20:i]) for i in range(20, len(rets) + 1, 5)]
    vols = sorted(v for v in vols if v)
    base = vols[len(vols) // 2] if vols else None
    ratio = (vol20 / base) if (vol20 and base) else None
    if ratio is None:
        volatility, regime = 0.0, "UNKNOWN"
    elif ratio >= 1.3:
        volatility, regime = -_clip((ratio - 1.0) / 0.8, 0, 1), "HIGH"
    elif ratio <= 0.8:
        volatility, regime = 0.3, "LOW"
    else:
        volatility, regime = 0.0, "NORMAL"
    lb = bars[-1]
    prev = closes[-2]
    gap_pct = round((lb["open"] - prev) / prev * 100, 3) if lb.get("open") and prev else None
    gap = _clip(gap_pct / 1.5) if gap_pct is not None else 0.0
    comps = {"trend": trend, "momentum": momentum, "volatility": volatility, "gap": gap}
    tw = sum(float(w.get(k, 0)) for k in comps) or 1.0
    score = round(sum(comps[k] * float(w.get(k, 0)) for k in comps) / tw * 100, 1)
    bull, bear = float(th.get("bullish", 20)), float(th.get("bearish", -20))
    sig = "BULLISH" if score >= bull else "BEARISH" if score <= bear else "NEUTRAL"
    sign = 1 if score > 0 else -1 if score < 0 else 0
    voted = [v for v in (trend, momentum, gap) if abs(v) > 0.05]
    agree = (sum(1 for v in voted if (v > 0) == (sign > 0)) / len(voted)) if (voted and sign) else 0.5
    completeness = min(1.0, len(closes) / 60)
    conf = round(min(100.0, 100 * (0.55 * agree + 0.25 * completeness + 0.20 * min(1.0, abs(score) / 50))), 1)
    return {"signal": sig, "score": score, "confidence": conf, "trend": round(trend, 3),
            "momentum": round(momentum, 3), "volatility": round(volatility, 3), "gap": round(gap, 3),
            "gap_pct": gap_pct, "vol_regime": regime, "bars": len(closes), "date": bars[-1]["date"],
            "components": {"close": last, "ema20": e20, "ema50": e50, "rsi14": rsi, "roc5_pct": roc5,
                           "vol20": vol20, "vol_ratio": ratio}}


def store_signal(conn, code, sig):
    conn.execute("INSERT INTO asia_signals (index_code,date,`signal`,confidence,score,trend,momentum,volatility,"
                 "gap_pct,gap,vol_regime,bars,components_json,computed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                 "ON CONFLICT(index_code,date) DO UPDATE SET `signal`=excluded.`signal`, confidence=excluded.confidence, "
                 "score=excluded.score, trend=excluded.trend, momentum=excluded.momentum, "
                 "volatility=excluded.volatility, gap_pct=excluded.gap_pct, gap=excluded.gap, "
                 "vol_regime=excluded.vol_regime, bars=excluded.bars, components_json=excluded.components_json, "
                 "computed_at=excluded.computed_at",
                 (code, sig["date"], sig["signal"], sig["confidence"], sig["score"], sig["trend"], sig["momentum"],
                  sig["volatility"], sig["gap_pct"], sig["gap"], sig["vol_regime"], sig["bars"],
                  json.dumps(sig["components"], default=float), datetime.now(IST).replace(tzinfo=None)))


# ── Pakistan geopolitical / risk-event flag (a separate factor, from news, not price) ──
def pakistan_risk(conn, now: datetime | None = None, cfg: dict | None = None) -> dict:
    """
    NONE / ELEVATED / HIGH from the stored, classified news: headlines that mention Pakistan and a risk term
    (border, strike, military, IMF default, ...) or are classified GEOPOLITICS with negative sentiment, in the
    last lookback_hours. ELEVATED at elevated_min such headlines, HIGH at high_min with an average sentiment at
    or below high_avg_sentiment, or any HIGH-importance negative one. Which classifier scored them is reported
    -- keyword-rule sentiment is weaker evidence than the AI's.
    """
    cfg = cfg or settings()
    pc = cfg.get("pakistan_risk") or {}
    now = (now or datetime.now(IST)).astimezone(IST).replace(tzinfo=None)
    since = now - timedelta(hours=int(pc.get("lookback_hours") or 48))
    kw = [k.lower() for k in pc.get("keywords") or []]
    risk = [k.lower() for k in pc.get("risk_terms") or []]
    try:
        rows = conn.execute("SELECT headline, sentiment, importance, category, classifier, fetched_at FROM "
                            "news_articles WHERE fetched_at>=? ORDER BY fetched_at DESC LIMIT 2000", (since,)).fetchall()
    except Exception as e:
        return {"level": "UNKNOWN", "reason": f"news unavailable: {e}", "count": 0, "headlines": []}
    hits = []
    for r in rows:
        r = dict(r)
        h = f" {str(r.get('headline') or '').lower()} "
        if not any(k in h for k in kw):
            continue
        s = r.get("sentiment")
        s = float(s) if s is not None else 0.0
        geo = str(r.get("category") or "").upper() in ("GEOPOLITICS", "GEOPOLITICAL")
        if any(t in h for t in risk) or (geo and s < 0):
            hits.append({**r, "sentiment": s})
    n = len(hits)
    avg = round(sum(h["sentiment"] for h in hits) / n, 3) if n else None
    severe = any(h["sentiment"] < 0 and str(h.get("importance") or "").upper() == "HIGH" for h in hits)
    if n >= int(pc.get("high_min") or 3) and avg is not None and avg <= float(pc.get("high_avg_sentiment") or -0.3) \
            or severe:
        level = "HIGH"
    elif n >= int(pc.get("elevated_min") or 1):
        level = "ELEVATED"
    else:
        level = "NONE"
    ai = sum(1 for h in hits if h.get("classifier") == "claude")
    return {"level": level, "count": n, "avg_sentiment": avg, "ai_classified": ai, "rule_classified": n - ai,
            "lookback_hours": int(pc.get("lookback_hours") or 48),
            "headlines": [{"headline": h["headline"], "sentiment": h["sentiment"], "importance": h.get("importance"),
                           "at": str(h.get("fetched_at"))[:16]} for h in hits[:8]]}


PK_RISK_VALUE = {"NONE": 50.0, "ELEVATED": 30.0, "HIGH": 0.0}


# ── combined Asia Sentiment Score ──────────────────────────────────────────
def asia_sentiment(signals: dict, pk: dict, cfg: dict) -> dict:
    """0-100 (50 neutral): the configured weighted mean of each market's score (mapped 50 + score/2) plus the
    Pakistan risk factor as its own weighted input (NONE 50, ELEVATED 30, HIGH 0). Renormalised over what is
    present; coverage is the share of weight that was."""
    w = cfg.get("weights") or {}
    th = cfg.get("thresholds") or {}
    parts, used, total = {}, 0.0, sum(float(v) for v in w.values()) or 1.0
    acc = 0.0
    confs = []
    for code, sig in signals.items():
        wt = float(w.get(code, 0))
        if sig is None or wt <= 0:
            continue
        v = 50 + sig["score"] / 2
        parts[code] = round(v, 2)
        acc += v * wt
        used += wt
        confs.append((sig["confidence"], wt))
    wpk = float(w.get("PK_GEO_RISK", 0))
    if wpk > 0 and pk.get("level") in PK_RISK_VALUE:
        parts["PK_GEO_RISK"] = PK_RISK_VALUE[pk["level"]]
        acc += PK_RISK_VALUE[pk["level"]] * wpk
        used += wpk
    if used <= 0:
        return {"score": None, "signal": None, "confidence": None, "coverage": 0.0, "components": parts}
    score = round(acc / used, 2)
    sig = ("BULLISH" if score >= float(th.get("sentiment_bullish", 60)) else
           "BEARISH" if score <= float(th.get("sentiment_bearish", 40)) else "NEUTRAL")
    coverage = round(used / total, 3)
    conf = round(sum(c * wt for c, wt in confs) / (sum(wt for _, wt in confs) or 1) * coverage, 1) if confs else None
    return {"score": score, "signal": sig, "confidence": conf, "coverage": coverage, "components": parts}


def sentiment_for(conn, d) -> float | None:
    """The Asia Sentiment Score for session date d (latest run that day), for Market Health. None if absent."""
    try:
        r = conn.execute("SELECT score FROM asia_sentiment WHERE date=? AND score IS NOT NULL "
                         "ORDER BY computed_at DESC LIMIT 1", (str(d)[:10],)).fetchone()
    except Exception:
        return None
    return float(r[0]) if r else None


# ── the job ────────────────────────────────────────────────────────────────
def run(label: str = "manual", now: datetime | None = None, cfg: dict | None = None) -> dict:
    """Fetch every index (primary, else fallback), store OHLC + session status, compute each market's signal,
    the Pakistan risk flag and the Asia Sentiment Score. Informational only -- places no order."""
    from db.schema import get_connection
    cfg = cfg or settings()
    now = now or datetime.now(IST)
    conn = get_connection()
    try:
        ensure_tables(conn)
        signals, report, rows, failed, fallbacks = {}, {}, 0, [], []
        for code in cfg["indexes"]:
            f = fetch_index(conn, code, cfg=cfg)
            n = store_bars(conn, code, f, cfg, now)
            rows += n
            if not f["bars"]:
                failed.append(code)
            elif f["fallback"]:
                fallbacks.append(f"{code}<-{f['source']}")
            sig = compute_signal(history(conn, code), cfg)
            if sig:
                store_signal(conn, code, sig)
            signals[code] = sig
            report[code] = {"source": f["source"], "rows": n, "signal": sig and sig["signal"],
                            "score": sig and sig["score"], "errors": f["errors"]}
        pk = pakistan_risk(conn, now, cfg)
        sent = asia_sentiment(signals, pk, cfg)
        d = str(now.astimezone(IST).date())
        conn.execute("INSERT INTO asia_sentiment (date,run_label,score,`signal`,confidence,coverage,pk_risk_level,"
                     "pk_risk_json,components_json,weights_json,computed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                     "ON CONFLICT(date,run_label) DO UPDATE SET score=excluded.score, `signal`=excluded.`signal`, "
                     "confidence=excluded.confidence, coverage=excluded.coverage, pk_risk_level=excluded.pk_risk_level, "
                     "pk_risk_json=excluded.pk_risk_json, components_json=excluded.components_json, "
                     "weights_json=excluded.weights_json, computed_at=excluded.computed_at",
                     (d, label, sent["score"], sent["signal"], sent["confidence"], sent["coverage"], pk["level"],
                      json.dumps(pk, default=str), json.dumps(sent["components"]), json.dumps(cfg.get("weights")),
                      now.astimezone(IST).replace(tzinfo=None)))
        conn.commit()
    finally:
        conn.close()
    log.info(f"🌏 Asia markets ({label}): sentiment {sent['score']} {sent['signal']} · PK risk {pk['level']}"
             + (f" · fallback {', '.join(fallbacks)}" if fallbacks else "")
             + (f" · FAILED {', '.join(failed)}" if failed else ""))
    status = "FAILED" if len(failed) == len(cfg["indexes"]) else "PARTIAL" if failed else "SUCCESS"
    return {"status": status, "rows": rows, "label": label, "sentiment": sent, "pakistan_risk": pk["level"],
            "failed": failed, "fallbacks": fallbacks, "markets": report,
            "error": f"no source answered for {', '.join(failed)}" if failed else None}


# ── calendar self-check ────────────────────────────────────────────────────
def calendar_check(conn, cfg: dict | None = None, days: int = 30, today: date | None = None) -> list:
    """Configured holidays that have a bar, and past weekdays with no bar that are not configured holidays,
    for each exchange over the last `days` days -- how an unverified holiday list gets corrected."""
    cfg = cfg or settings()
    out = []
    for code, spec in cfg["indexes"].items():
        ex = cfg["exchanges"][spec["exchange"]]
        t = today or local_date(spec["exchange"], cfg=cfg)
        have = {r["date"] for r in history(conn, code, n=days * 2)}
        if not have:
            continue
        first = min(have)
        for i in range(1, days + 1):
            d = t - timedelta(days=i)
            if str(d) < first or d.weekday() not in (ex.get("weekdays") or [0, 1, 2, 3, 4]):
                continue
            listed = str(d) in set(ex.get("holidays") or [])
            if listed and str(d) in have:
                out.append({"index": code, "exchange": spec["exchange"], "date": str(d),
                            "issue": "listed as a holiday but the index has a bar"})
            elif not listed and str(d) not in have:
                out.append({"index": code, "exchange": spec["exchange"], "date": str(d),
                            "issue": "weekday with no bar and not listed as a holiday"})
    return out


# ── read side (dashboard, mobile, report) ──────────────────────────────────
def snapshot(conn, now: datetime | None = None, cfg: dict | None = None) -> dict:
    """Each market's last value, % change, signal and session status (now), plus the Asia Sentiment Score."""
    cfg = cfg or settings()
    now = now or datetime.now(IST)
    try:
        ensure_tables(conn)
    except Exception:
        pass
    markets = []
    for code, spec in cfg["indexes"].items():
        r = conn.execute("SELECT date, close, chg_pct, prev_close, source, is_fallback, fetched_at FROM "
                         "global_indices_daily WHERE index_code=? ORDER BY date DESC LIMIT 1", (code,)).fetchone()
        s = conn.execute("SELECT date, `signal`, confidence, score, trend, momentum, volatility, gap_pct, vol_regime "
                         "FROM asia_signals WHERE index_code=? ORDER BY date DESC LIMIT 1", (code,)).fetchone()
        r, s = (dict(r) if r else {}), (dict(s) if s else {})
        markets.append({"code": code, "name": spec["name"], "market": spec["market"], "exchange": spec["exchange"],
                        "session_status": session_status(spec["exchange"], now, cfg),
                        "session_ist": session_times_ist(spec["exchange"], local_date(spec["exchange"], now, cfg), cfg),
                        "date": r.get("date"), "last": r.get("close"), "chg_pct": r.get("chg_pct"),
                        "source": r.get("source"), "fallback": bool(r.get("is_fallback")),
                        "fetched_at": str(r.get("fetched_at") or "")[:19] or None,
                        "signal": s.get("signal"), "confidence": s.get("confidence"), "score": s.get("score"),
                        "trend": s.get("trend"), "momentum": s.get("momentum"), "volatility": s.get("volatility"),
                        "gap_pct": s.get("gap_pct"), "vol_regime": s.get("vol_regime")})
    sent = conn.execute("SELECT * FROM asia_sentiment ORDER BY computed_at DESC LIMIT 1").fetchone()
    sent = dict(sent) if sent else None
    if sent:
        for k in ("pk_risk_json", "components_json", "weights_json"):
            try:
                sent[k[:-5]] = json.loads(sent.pop(k) or "null")
            except Exception:
                sent[k[:-5]] = None
    return {"now_ist": now.astimezone(IST).strftime("%Y-%m-%d %H:%M"), "markets": markets, "sentiment": sent,
            "informational_only": True}


def source_health(conn) -> list:
    try:
        ensure_tables(conn)
        return [dict(r) for r in conn.execute("SELECT * FROM asia_source_health ORDER BY index_code, kind").fetchall()]
    except Exception:
        return []


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("cmd", choices=["verify", "run", "status"])
    ap.add_argument("--label", default="manual")
    a = ap.parse_args(argv)
    if a.cmd == "verify":
        for r in verify_sources():
            print(f"{'OK     ' if r['ok'] else 'FLAGGED'} {r['index']:<7} {r['kind']}:{r['ticker']:<8} "
                  f"{r['rows']:>3} bars {r['last_date'] or '':<10} {r['error'] or ''}")
    elif a.cmd == "run":
        print(json.dumps(run(a.label), indent=2, default=str))
    else:
        from db.schema import get_connection
        c = get_connection()
        try:
            print(json.dumps(snapshot(c), indent=2, default=str))
        finally:
            c.close()


if __name__ == "__main__":
    main()
