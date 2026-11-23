"""
9:00 AM Pre-Open Checklist (owner prompt 22 Nov 2026, PO-CL-01..PO-CL-14). Four checks before the 09:15 open,
stored once per run in `preopen_signals`, scored against the real open and close every evening. Informational and
paper-only: nothing here imports orders/, places, sizes or confirms an order.

  1. US cues      Nasdaq Composite (^IXIC) and Dow Jones (^DJI) previous-session close change, plus the Nasdaq 100
                  (NQ=F) and Dow (YM=F) futures against their previous settlement at run time (Yahoo, through
                  data/markets.py's yfinance wrapper: breaker + retry). Each leg: above +us_band_pct green, below
                  -us_band_pct red, else flat. Vote = sign(green legs - red legs): +1 / 0 / -1.
                  Fallbacks: the closes stored by the pre-market job (global_markets), the NQ snapshot of
                  research/global_sync.py (global_snapshot "pre"); a futures quote older than
                  us_futures_max_age_min is shown as stale and left out.
  2. India VIX    above vix_threshold (15) "Volatile", at or below "Stable". NOT a vote: a volatility modifier with
                  advisory text only -- size x volatile_size_multiplier, stop width x volatile_stop_multiplier, "avoid
                  option buying / prefer defined-risk". It does not change autopick, the arms or any stop (owner
                  decision D2, 22 Nov 2026). ATIP's other VIX bands are untouched: strategy_engine/regime.py vol_regime
                  13 / 20, the CRI MarketWeakness steps 18 / 25, the Telegram spike alert 25, Market Health's inverted
                  8..35 scale. Source: Dhan IDX_I id 21, else the stored prior close (scores.engine.get_vix).
  3. NSE A/D      NSE's pre-open call auction (09:00-09:08) across ALL securities (key=ALL): advances vs declines by
                  indicative price. Read at 09:00 and again at 09:09, after order matching (order entry closes at a
                  random second between 09:07 and 09:08, so 09:08:00 can read an unfinished book -- conflict C3).
                  Before NSE publishes today's book it still serves the previous session's (data/preopen.py refuses
                  that page as STALE), so the check falls back to the previous session's whole-market A/D from NSE's
                  Bhavcopy (lake nse_cm_bhavcopy, EQ series), else ATIP's own universe (market_health), labelled
                  "prior-day". ratio = advances / declines: above ad_ratio_bull +1, below ad_ratio_bear -1, else 0.
  4. GIFT Nifty   the NSE IX near-month GIFT Nifty future from NSE's marketStatus API ("giftnifty" block), or a
                  configured JSON fallback adapter. Dhan's IDX_I 5024 quote is NOT used: it is frozen (one value all
                  day; 23,240 from 2 to 6 Nov 2026 while the Nifty went 23,441 -> 24,537).
                  The vote uses the basis-adjusted move (conflict C2): GIFT now against GIFT at 15:30 IST on the
                  previous session from the same source (captured at 15:31 by capture_gift_close() into
                  global_snapshot, series "gift_nifty"), since GIFT is a future and its carry premium over spot is not
                  news; without that reference, NSE's own day change of the contract; without that, the raw "GIFT vs
                  previous Nifty 50 close", labelled "basis not removed". The raw comparison is always stored and
                  shown. |move| below gift_band_pct (0.15 %) "Flat" (0), else "Gap-Up" (+1) / "Gap-Down" (-1).
                  gift_vote_basis "raw" votes on the raw comparison instead.

  Bias            vote sum of US + A/D + GIFT: >= +bias_min_votes "Bullish", <= -bias_min_votes "Bearish", else
                  "Neutral". A check with no usable value does not vote and is listed in caveats.
  Statuses        LIVE (fetched now), FALLBACK (a labelled alternative that votes: prior-day A/D, stored US closes),
                  STALE (shown, older than its age limit, no vote), UNAVAILABLE (nothing, no vote). A fetch never
                  raises out of run(): it becomes a status and a caveat.
  Runs            "preliminary" (08:15 IST, before the pre-market report and the 08:30 brief), "0900" and "0909" on
                  trading days only (utils/trading_calendar.is_trading_day: weekends and NSE holidays skipped).
  Learning loop   evaluate() fills the actual open (first index_levels Nifty reading 09:15-09:30, as
                  research/market_pulse.evaluate_gaps), the open's class with the same band, the close and the day's
                  direction; record() gives each check's and the bias's hit rate over the last 20 / 60 evaluated days.

Config (atip_data/config.json "preopen_checklist", every key optional): see DEFAULTS.
CLI: python -m strategy.preopen_checklist run [--label 0900] | evaluate | record | show | gift-close
"""

from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger("atip.preopen_checklist")

IST = ZoneInfo("Asia/Kolkata")
RUN_LABELS = ("preliminary", "0900", "0909")
FINAL_ORDER = {"0909": 0, "0900": 1, "preliminary": 2}          # the run a day is scored on: latest first

DEFAULTS = {
    "enabled": True,
    "us_band_pct": 0.10,              # a US leg within +-0.10 % is flat
    "vix_threshold": 15.0,            # India VIX above: Volatile; at or below: Stable
    "ad_ratio_bull": 1.0,             # advances / declines above: +1
    "ad_ratio_bear": 1.0,             # below: -1 (equal: 0); set 1.1 / 0.9 for a neutral band
    "gift_band_pct": 0.15,            # |GIFT move| below: Flat open
    "gift_vote_basis": "adjusted",    # "adjusted" (basis removed, C2) or "raw" (GIFT vs previous Nifty close)
    "bias_min_votes": 1,              # vote sum >= +1 Bullish, <= -1 Bearish
    "volatile_size_multiplier": 0.5,  # advisory only
    "volatile_stop_multiplier": 1.5,  # advisory only
    "us_close_max_age_days": 4,       # a US close older than this (calendar days) is stale
    "us_futures_max_age_min": 90,     # an NQ / YM quote older than this is stale
    "gift_max_age_min": 30,           # a GIFT quote older than this is stale
    "telegram": True,                 # the 09:00 line (and a 09:09 line when the read changed)
    "gift_sources": ["nse_marketstatus", "json_url"],
    "gift_json_url": "",              # the fallback adapter, OFF until a URL is set
    "gift_json_price_path": "",       # dotted path to the price, e.g. "data.0.lastPrice"
    "gift_json_time_path": "",        # dotted path to the quote time (optional)
}

US_TICKERS = {"nasdaq": "^IXIC", "dow": "^DJI", "nq": "NQ=F", "ym": "YM=F"}
US_LABELS = {"nasdaq": "Nasdaq", "dow": "Dow", "nq": "NQ fut", "ym": "YM fut"}
NSE_MARKET_STATUS = "https://www.nseindia.com/api/marketStatus"
NSE_PREOPEN_ALL = "https://www.nseindia.com/api/market-data-pre-open?key=ALL"
GIFT_SERIES = "gift_nifty"            # global_snapshot series of the 15:31 GIFT reference
EVAL_GIVE_UP_DAYS = 7

DDL = (
    """CREATE TABLE IF NOT EXISTS preopen_signals (
        date DATE NOT NULL, run_label TEXT NOT NULL,
        nasdaq_close REAL, nasdaq_chg_pct REAL, dow_close REAL, dow_chg_pct REAL, us_close_date TEXT,
        nq_price REAL, nq_chg_pct REAL, ym_price REAL, ym_chg_pct REAL, us_futures_as_of TEXT,
        us_green INTEGER, us_red INTEGER, us_vote INTEGER, us_status TEXT, us_source TEXT,
        vix REAL, vix_regime TEXT, vix_threshold REAL, vix_status TEXT, vix_source TEXT, vix_as_of TEXT,
        size_multiplier REAL, stop_multiplier REAL, options_note TEXT,
        advances INTEGER, declines INTEGER, unchanged INTEGER, ad_ratio REAL, ad_basis TEXT, ad_vote INTEGER,
        ad_status TEXT, ad_source TEXT, ad_as_of TEXT,
        gift_price REAL, gift_expiry TEXT, gift_ref REAL, gift_ref_source TEXT, nifty_prev_close REAL,
        gift_vs_close_pct REAL, gift_move_pct REAL, gift_move_basis TEXT, gift_vote INTEGER, expected_open TEXT,
        gift_status TEXT, gift_source TEXT, gift_as_of TEXT,
        vote_sum INTEGER, votes_counted INTEGER, bias TEXT, config_json TEXT, caveats_json TEXT, fetched_at_ist TEXT,
        actual_open REAL, actual_gap_pct REAL, actual_open_class TEXT, nifty_close REAL, day_chg_pct REAL,
        day_direction TEXT, evaluated_at_ist TEXT, eval_note TEXT,
        PRIMARY KEY (date, run_label))""",
    "CREATE INDEX IF NOT EXISTS idx_preopen_signals_eval ON preopen_signals(evaluated_at_ist)",
)

RUN_COLUMNS = (
    "nasdaq_close", "nasdaq_chg_pct", "dow_close", "dow_chg_pct", "us_close_date", "nq_price", "nq_chg_pct",
    "ym_price", "ym_chg_pct", "us_futures_as_of", "us_green", "us_red", "us_vote", "us_status", "us_source",
    "vix", "vix_regime", "vix_threshold", "vix_status", "vix_source", "vix_as_of", "size_multiplier",
    "stop_multiplier", "options_note", "advances", "declines", "unchanged", "ad_ratio", "ad_basis", "ad_vote",
    "ad_status", "ad_source", "ad_as_of", "gift_price", "gift_expiry", "gift_ref", "gift_ref_source",
    "nifty_prev_close", "gift_vs_close_pct", "gift_move_pct", "gift_move_basis", "gift_vote", "expected_open",
    "gift_status", "gift_source", "gift_as_of", "vote_sum", "votes_counted", "bias", "config_json", "caveats_json",
    "fetched_at_ist",
)
VOTING = {"LIVE", "FALLBACK"}


# ── config / helpers ─────────────────────────────────────────────────────────────────────────────────────────

def settings() -> dict:
    """DEFAULTS overlaid with config.json "preopen_checklist"; bad values fall back to the default with a warning."""
    try:
        from ops.config import load
        raw = load().get("preopen_checklist") or {}
    except Exception:
        raw = {}
    out = dict(DEFAULTS)
    for k, v in (raw.items() if isinstance(raw, dict) else ()):
        if k not in DEFAULTS:
            continue
        d = DEFAULTS[k]
        try:
            if isinstance(d, bool):
                out[k] = v is not False if k == "enabled" else bool(v)
            elif isinstance(d, (int, float)) and not isinstance(d, bool):
                f = float(v)
                if not math.isfinite(f) or f < 0:
                    raise ValueError(v)
                out[k] = int(f) if isinstance(d, int) else f
            elif isinstance(d, list):
                out[k] = [str(x) for x in v] if isinstance(v, (list, tuple)) else d
            else:
                out[k] = str(v)
        except (TypeError, ValueError):
            log.warning(f"  preopen_checklist.{k}={v!r} is not valid; using {d!r}")
    if out["gift_vote_basis"] not in ("adjusted", "raw"):
        out["gift_vote_basis"] = "adjusted"
    out["bias_min_votes"] = max(1, int(out["bias_min_votes"]))
    return out


def ensure_tables(conn):
    for d in DDL:
        conn.execute(d)


def now_ist() -> datetime:
    return datetime.now(IST)


def _ist(dt: datetime) -> str:
    """'YYYY-MM-DD HH:MM:SS' in IST (an aware time is converted; a naive one is taken as IST already)."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(IST)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _f(v):
    if isinstance(v, str):
        v = v.replace(",", "").replace("%", "").strip()
        if v in ("", "-", "--"):
            return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _r(v, n=3):
    return None if v is None else round(float(v), n)


def _d(v) -> date:
    return v if isinstance(v, date) and not isinstance(v, datetime) else date.fromisoformat(str(v)[:10])


def _rows(conn, sql, args=()) -> list:
    try:
        cur = conn.execute(sql, args)
        cols = [c[0] for c in cur.description] if cur.description else []
        return [dict(zip(cols, tuple(r))) for r in cur.fetchall()]
    except Exception as e:
        log.debug(f"  preopen query: {e}")
        return []


def prev_session(d: date) -> date:
    from utils.trading_calendar import is_trading_day
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def parse_time(s, default_tz=IST) -> datetime | None:
    """NSE / ISO / pandas time text -> aware datetime (naive text is taken as IST)."""
    if s is None or s == "":
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=default_tz)
    txt = str(s).strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(txt, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=default_tz)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=default_tz)
    except ValueError:
        return None


def sign_band(pct, band) -> int | None:
    """+1 above +band, -1 below -band, 0 inside; None without a value."""
    if pct is None:
        return None
    return 1 if pct > band else -1 if pct < -band else 0


def _pct(a, b):
    return (a / b - 1) * 100 if a is not None and b else None


# ── sources (each injectable; tests pass fakes) ──────────────────────────────────────────────────────────────

def _yahoo_us() -> dict:
    """{key: {"last", "prev", "as_of"}} for ^IXIC, ^DJI, NQ=F, YM=F. Indices: the last two daily closes (the last
    is the previous US session's, at 09:00 IST the US cash market is shut). Futures: the latest 5-minute price and
    its time against the previous settlement (the daily close before the session in progress)."""
    from data.markets import _yf_download
    out = {}
    daily = _yf_download(list(US_TICKERS.values()), period="7d", interval="1d", group_by="ticker", progress=False,
                         auto_adjust=True)

    def closes(df, t):
        try:
            return df[t]["Close"].dropna()
        except Exception:
            return None
    for k in ("nasdaq", "dow"):
        s = closes(daily, US_TICKERS[k])
        if s is not None and len(s) >= 2:
            out[k] = {"last": float(s.iloc[-1]), "prev": float(s.iloc[-2]), "as_of": str(s.index[-1])[:10]}
    try:
        intra = _yf_download([US_TICKERS["nq"], US_TICKERS["ym"]], period="2d", interval="5m", group_by="ticker",
                             progress=False, auto_adjust=True)
    except Exception as e:
        log.warning(f"  US futures 5-minute quotes: {e}")
        intra = None
    for k in ("nq", "ym"):
        d = closes(daily, US_TICKERS[k])
        if d is None or len(d) < 2:
            continue
        last, ts = float(d.iloc[-1]), str(d.index[-1])
        s = closes(intra, US_TICKERS[k]) if intra is not None else None
        if s is not None and len(s):
            last, ts = float(s.iloc[-1]), str(s.index[-1])
        # the daily row of the session in progress carries the live price; the settlement is the row before it
        prev = float(d.iloc[-2]) if abs(float(d.iloc[-1]) / last - 1) < 0.0005 else float(d.iloc[-1])
        out[k] = {"last": last, "prev": prev, "as_of": ts}
    return out


def _dhan_quotes() -> dict:
    """{"india_vix": {...}, "nifty50": {...}} from Dhan IDX_I (ltp, prev_close)."""
    from data.dhan import fetch_index_quotes
    df = fetch_index_quotes(["india_vix", "nifty50"])
    if df is None or getattr(df, "empty", True):
        return {}
    return {r["index"]: r for r in df.to_dict("records")}


def _nse_json(url):
    from data.nse_api import client
    return client().json(url)


def _nse_preopen_all() -> dict | None:
    return _nse_json(NSE_PREOPEN_ALL)


def parse_gift_marketstatus(payload) -> dict | None:
    """NSE marketStatus -> {"price", "chg_pct", "expiry", "as_of"} from its "giftnifty" block (keys read case-
    insensitively: LASTPRICE, PERCHANGE, EXPIRYDATE, TIMESTMP); None when absent or without a price."""
    if not isinstance(payload, dict):
        return None
    g = None
    for k, v in payload.items():
        if str(k).lower().replace("_", "") == "giftnifty":
            g = v
            break
    if isinstance(g, list):
        g = g[0] if g else None
    if not isinstance(g, dict):
        return None
    k = {str(a).lower().replace("_", ""): v for a, v in g.items()}
    price = _f(k.get("lastprice") if k.get("lastprice") is not None else k.get("ltp", k.get("last")))
    if not price:
        return None
    ts = k.get("timestmp") or k.get("timestamp") or k.get("lastupdatetime") or k.get("tradedate")
    return {"price": price, "chg_pct": _f(k.get("perchange", k.get("pchange"))), "expiry": k.get("expirydate"),
            "as_of": parse_time(ts), "source": "NSE marketStatus (GIFT Nifty, NSE IX)"}


def _dig(obj, path: str):
    for part in [p for p in str(path or "").split(".") if p]:
        if isinstance(obj, list):
            try:
                obj = obj[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
    return obj


def _gift_nse() -> dict | None:
    return parse_gift_marketstatus(_nse_json(NSE_MARKET_STATUS))


def _gift_json_url(cfg) -> dict | None:
    """The fallback adapter: any JSON endpoint the owner configures (OFF until gift_json_url is set). Isolated here
    so a fragile third-party source never reaches the rest of ATIP."""
    url = cfg.get("gift_json_url")
    if not url:
        return None
    import requests
    r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0 ATIP"})
    r.raise_for_status()
    payload = r.json()
    price = _f(_dig(payload, cfg.get("gift_json_price_path")))
    if not price:
        return None
    return {"price": price, "chg_pct": None, "expiry": None,
            "as_of": parse_time(_dig(payload, cfg.get("gift_json_time_path"))), "source": f"fallback adapter ({url[:60]})"}


def _gift_quote(cfg) -> dict | None:
    """The first configured GIFT source that answers with a price; errors are logged, never raised."""
    for name in cfg.get("gift_sources") or []:
        try:
            q = _gift_nse() if name == "nse_marketstatus" else _gift_json_url(cfg) if name == "json_url" else None
        except Exception as e:
            log.warning(f"  GIFT source {name}: {e}")
            q = None
        if q:
            return q
    return None


class Sources:
    """The four fetchers. Defaults are the real ones; tests replace any of them."""

    def __init__(self, us=None, dhan=None, preopen=None, gift=None):
        self.us = us or _yahoo_us
        self.dhan = dhan or _dhan_quotes
        self.preopen = preopen or _nse_preopen_all
        self.gift = gift


def _call(fn, *a):
    try:
        return fn(*a), None
    except Exception as e:
        first = (str(e).strip().splitlines() or [""])[0]
        return None, f"{type(e).__name__}: {first[:160]}"


# ── the four checks ──────────────────────────────────────────────────────────────────────────────────────────

def check_us(conn, now: datetime, cfg: dict, src: Sources) -> dict:
    data, err = _call(src.us)
    data = data or {}
    band, caveats, legs, source = cfg["us_band_pct"], [], {}, "Yahoo (yfinance)"
    stored = None
    if not data.get("nasdaq") or not data.get("dow"):
        stored = (_rows(conn, "SELECT date, nasdaq, nasdaq_chg, dow, dow_chg FROM global_markets WHERE "
                              "nasdaq_chg IS NOT NULL AND dow_chg IS NOT NULL AND date<=? ORDER BY date DESC, id DESC "
                              "LIMIT 1", (str(now.date()),)) or [None])[0]
    out = {"us_close_date": None}
    for k in ("nasdaq", "dow"):
        q = data.get(k)
        if q and q.get("prev"):
            out[f"{k}_close"], out[f"{k}_chg_pct"] = _r(q["last"], 2), _r(_pct(q["last"], q["prev"]))
            out["us_close_date"] = out["us_close_date"] or str(q.get("as_of"))[:10]
        elif stored and stored.get(f"{k}_chg") is not None:
            out[f"{k}_close"], out[f"{k}_chg_pct"] = _r(stored.get(k), 2), _r(stored[f"{k}_chg"])
            out["us_close_date"] = f"stored {str(stored['date'])[:10]}"
            source = "Yahoo + stored global_markets"
    close_ok = True
    cd = out.get("us_close_date")
    if cd:
        try:
            close_ok = (now.date() - _d(cd.replace("stored ", ""))).days <= cfg["us_close_max_age_days"]
        except ValueError:
            pass
    if not close_ok:
        caveats.append(f"US closes are from {cd}, older than {cfg['us_close_max_age_days']} days")
    for k in ("nq", "ym"):
        q = data.get(k)
        if not q or not q.get("prev"):
            continue
        out[f"{k}_price"], out[f"{k}_chg_pct"] = _r(q["last"], 2), _r(_pct(q["last"], q["prev"]))
        t = parse_time(q.get("as_of"), default_tz=ZoneInfo("UTC"))
        if t:
            out["us_futures_as_of"] = max(out.get("us_futures_as_of") or "", _ist(t))
            if (now - t).total_seconds() / 60 > cfg["us_futures_max_age_min"]:
                caveats.append(f"{US_LABELS[k]} quote is from {_ist(t)} IST, older than "
                               f"{cfg['us_futures_max_age_min']} min: not counted")
                out[f"{k}_stale"] = True
    if not data.get("nq"):                       # global_sync's 08:42 NQ snapshot, the same day
        pre = (_rows(conn, "SELECT price, quote_ts FROM global_snapshot WHERE date=? AND label='pre' AND series='nq'",
                     (str(now.date()),)) or [None])[0]
        cl = (_rows(conn, "SELECT price FROM global_snapshot WHERE date=? AND label='close' AND series='nq'",
                    (str(prev_session(now.date())),)) or [None])[0]
        if pre and cl and cl.get("price"):
            out["nq_price"], out["nq_chg_pct"] = _r(pre["price"], 2), _r(_pct(pre["price"], cl["price"]))
            out["us_futures_as_of"] = str(pre.get("quote_ts") or "")[:19]
            caveats.append("NQ from the 08:42 snapshot against yesterday's 15:31 one (global_sync)")
            source += " + global_snapshot"
    for k in ("nasdaq", "dow", "nq", "ym"):
        usable = out.get(f"{k}_chg_pct") is not None and not out.get(f"{k}_stale") and (k in ("nq", "ym") or close_ok)
        if usable:
            legs[k] = sign_band(out[f"{k}_chg_pct"], band)
    out.pop("nq_stale", None)
    out.pop("ym_stale", None)
    green, red = sum(1 for v in legs.values() if v > 0), sum(1 for v in legs.values() if v < 0)
    out.update(us_green=green, us_red=red)
    if not legs:
        out.update(us_vote=None, us_status="UNAVAILABLE", us_source=source)
        caveats.append("no US close or futures quote" + (f" ({err})" if err else ""))
    else:
        out.update(us_vote=(green > red) - (green < red),
                   us_status="LIVE" if data and source == "Yahoo (yfinance)" else "FALLBACK", us_source=source)
        missing = [US_LABELS[k] for k in US_TICKERS if k not in legs]
        if missing:
            caveats.append("US vote without " + ", ".join(missing))
    out["_legs"] = legs
    return {"values": out, "caveats": caveats}


def check_vix(conn, now: datetime, cfg: dict, quotes: dict) -> dict:
    caveats = []
    q = (quotes or {}).get("india_vix") or {}
    vix, source, as_of, status = _f(q.get("ltp")), "Dhan IDX_I (India VIX, id 21)", _ist(now), "LIVE"
    if not vix:
        d = prev_session(now.date())
        try:
            from scores.engine import get_vix
            vix = get_vix(d, conn)
        except Exception:
            vix = None
        source, as_of, status = "stored prior close (index_levels / prices_daily INDIAVIX)", str(d), "FALLBACK"
        if vix:
            caveats.append(f"India VIX from the stored close of {d}, not a live quote")
    thr = cfg["vix_threshold"]
    if not vix:
        return {"values": {"vix": None, "vix_regime": "Unknown", "vix_threshold": thr, "vix_status": "UNAVAILABLE",
                           "vix_source": "none", "vix_as_of": None, "size_multiplier": None, "stop_multiplier": None,
                           "options_note": None}, "caveats": ["no India VIX value"]}
    volatile = vix > thr
    return {"values": {
        "vix": _r(vix, 2), "vix_regime": "Volatile" if volatile else "Stable", "vix_threshold": thr,
        "vix_status": status, "vix_source": source, "vix_as_of": as_of,
        "size_multiplier": cfg["volatile_size_multiplier"] if volatile else 1.0,
        "stop_multiplier": cfg["volatile_stop_multiplier"] if volatile else 1.0,
        "options_note": ("Volatile: avoid buying options outright (premiums are rich); prefer defined-risk spreads"
                         if volatile else "Stable: normal sizing; option premiums are not stretched")},
        "caveats": caveats}


def preopen_counts(payload, today: date) -> dict | None:
    """NSE's key=ALL pre-open payload -> {"advances", "declines", "unchanged", "nse_time"} when it is TODAY's book
    (NSE serves the previous session's until about 09:00); None otherwise. Uses NSE's own top-level counts, else
    counts the items' indicative change."""
    if not isinstance(payload, dict):
        return None
    from data.preopen import parse
    items = payload.get("data") if isinstance(payload.get("data"), list) else []
    parsed = [p for p in (parse(i) for i in items) if p]
    stamps = sorted(p["nse_time"] for p in parsed if p.get("nse_time"))
    if not stamps or stamps[-1][:10] != str(today):
        return None
    adv, dec, unc = _f(payload.get("advances")), _f(payload.get("declines")), _f(payload.get("unchanged"))
    if adv is None or dec is None:
        chg = [p["iep_chg_pct"] for p in parsed if p.get("iep_chg_pct") is not None]
        adv, dec, unc = sum(1 for c in chg if c > 0), sum(1 for c in chg if c < 0), sum(1 for c in chg if c == 0)
    return {"advances": int(adv), "declines": int(dec), "unchanged": int(unc or 0), "nse_time": stamps[-1]}


def prior_day_ad(conn, d: date) -> dict | None:
    """The session d's whole-market A/D from NSE's Bhavcopy (EQ series), else ATIP's universe (market_health)."""
    try:
        from data import lake
        df = lake.read("nse_cm_bhavcopy", d, d, columns=["symbol", "series", "close", "prev_close"], conn=conn)
        if df is not None and not df.empty:
            df = df[df["series"].astype(str).str.strip().str.upper() == "EQ"]
            df = df[(df["close"] > 0) & (df["prev_close"] > 0)]
            if len(df):
                return {"advances": int((df["close"] > df["prev_close"]).sum()),
                        "declines": int((df["close"] < df["prev_close"]).sum()),
                        "unchanged": int((df["close"] == df["prev_close"]).sum()),
                        "source": f"NSE Bhavcopy {d} (all EQ, {len(df)} stocks)", "as_of": str(d)}
    except Exception as e:
        log.debug(f"  prior-day A/D from the Bhavcopy: {e}")
    r = (_rows(conn, "SELECT date, advances, declines, breadth_universe FROM market_health WHERE date=? AND "
                     "advances IS NOT NULL AND declines IS NOT NULL", (str(d),)) or [None])[0]
    if r:
        return {"advances": int(r["advances"]), "declines": int(r["declines"]), "unchanged": None,
                "source": f"ATIP universe {d} ({r.get('breadth_universe') or '?'} stocks, market_health)",
                "as_of": str(d)}
    return None


def check_ad(conn, now: datetime, cfg: dict, src: Sources) -> dict:
    caveats = []
    payload, err = _call(src.preopen)
    live = preopen_counts(payload, now.date()) if payload else None
    if live:
        basis = "pre-open (after matching)" if live["nse_time"][11:16] >= "09:07" else "pre-open (indicative)"
        adv, dec, unc, source, as_of, status = (live["advances"], live["declines"], live["unchanged"],
                                                "NSE pre-open, all securities (key=ALL)", live["nse_time"], "LIVE")
    else:
        if payload is None:
            caveats.append("NSE pre-open page not reached" + (f" ({err})" if err else ""))
        else:
            caveats.append("NSE pre-open page still shows the previous session (published from about 09:00)")
        snap = None
        try:                                       # data/preopen.py's 09:09 capture (NIFTY + FO keys), if stored
            from data.preopen import summary
            s = summary(conn, day=now.date())
            snap = s.get("all") if s.get("status") == "OK" and str(s.get("date")) == str(now.date()) else None
        except Exception:
            snap = None
        if snap and (snap.get("advances") or snap.get("declines")):
            basis, status = "pre-open (F&O + Nifty 50 set)", "FALLBACK"
            adv, dec, unc = snap["advances"], snap["declines"], snap.get("unchanged")
            source, as_of = f"preopen_snapshot ({snap.get('stocks')} stocks, data/preopen.py)", s.get("nse_time")
        else:
            pd_ = prior_day_ad(conn, prev_session(now.date()))
            if not pd_:
                return {"values": {"advances": None, "declines": None, "unchanged": None, "ad_ratio": None,
                                   "ad_basis": None, "ad_vote": None, "ad_status": "UNAVAILABLE", "ad_source": "none",
                                   "ad_as_of": None}, "caveats": caveats + ["no pre-open or prior-day A/D"]}
            basis, status = "prior-day", "FALLBACK"
            adv, dec, unc, source, as_of = pd_["advances"], pd_["declines"], pd_["unchanged"], pd_["source"], pd_["as_of"]
            caveats.append(f"A/D is PRIOR-DAY ({as_of}), not today's pre-open")
    ratio = (adv / dec) if dec else (float("inf") if adv else None)
    vote = None if ratio is None else 1 if ratio > cfg["ad_ratio_bull"] else -1 if ratio < cfg["ad_ratio_bear"] else 0
    return {"values": {"advances": adv, "declines": dec, "unchanged": unc,
                       "ad_ratio": None if ratio is None else (999.0 if ratio == float("inf") else _r(ratio)),
                       "ad_basis": basis, "ad_vote": vote, "ad_status": status, "ad_source": source,
                       "ad_as_of": as_of}, "caveats": caveats}


def nifty_prev_close(conn, d: date, quotes: dict) -> tuple:
    """(the previous session's Nifty 50 close, where from)."""
    p = prev_session(d)
    r = (_rows(conn, "SELECT time, nifty50 FROM index_levels WHERE date=? AND nifty50>0 AND time<='15:30:59' "
                     "ORDER BY time DESC LIMIT 1", (str(p),)) or [None])[0]
    if r:
        return float(r["nifty50"]), f"index_levels {p} {str(r['time'])[:5]}"
    q = (quotes or {}).get("nifty50") or {}
    v = _f(q.get("prev_close")) or _f(q.get("ltp"))
    if v:
        return v, "Dhan IDX_I Nifty 50 previous close"
    r = (_rows(conn, "SELECT close FROM prices_daily WHERE symbol='NIFTY50' AND date=? AND close>0", (str(p),)) or [None])[0]
    if r:
        return float(r["close"]), f"prices_daily NIFTY50 {p}"
    return None, None


def gift_reference(conn, d: date) -> dict | None:
    """GIFT at 15:31 IST on the session before d, from capture_gift_close() (global_snapshot)."""
    p = prev_session(d)
    r = (_rows(conn, "SELECT price, quote_ts, captured_at FROM global_snapshot WHERE date=? AND label='close' AND "
                     "series=? AND price>0", (str(p), GIFT_SERIES)) or [None])[0]
    return {"price": float(r["price"]), "source": f"GIFT at {str(r.get('quote_ts') or r.get('captured_at'))[:16]} "
                                                  f"IST ({p})"} if r else None


def check_gift(conn, now: datetime, cfg: dict, src: Sources, quotes: dict) -> dict:
    caveats = []
    getter = src.gift or (lambda: _gift_quote(cfg))
    q, err = _call(getter)
    prev_close, pc_src = nifty_prev_close(conn, now.date(), quotes)
    base = {"gift_price": None, "gift_expiry": None, "gift_ref": None, "gift_ref_source": None,
            "nifty_prev_close": _r(prev_close, 2), "gift_vs_close_pct": None, "gift_move_pct": None,
            "gift_move_basis": None, "gift_vote": None, "expected_open": "Unknown"}
    if not q:
        caveats.append("no GIFT Nifty quote" + (f" ({err})" if err else "") +
                       " -- Dhan's GIFT feed is frozen and is not used")
        return {"values": {**base, "gift_status": "UNAVAILABLE", "gift_source": "none", "gift_as_of": None},
                "caveats": caveats}
    price, as_of = q["price"], q.get("as_of")
    status = "LIVE"
    if as_of is None:
        caveats.append("the GIFT source gave no quote time; taken as of the fetch")
        as_of = now
    elif (now - as_of).total_seconds() / 60 > cfg["gift_max_age_min"]:
        status = "STALE"
        caveats.append(f"GIFT quote is from {_ist(as_of)} IST, older than {cfg['gift_max_age_min']} min: not counted")
    raw = _pct(price, prev_close)
    ref = gift_reference(conn, now.date())
    if ref:
        move, basis = _pct(price, ref["price"]), "vs GIFT 15:30 prev session"
    elif q.get("chg_pct") is not None:
        move, basis = q["chg_pct"], "NSE day change of the contract"
        caveats.append("no 15:31 GIFT reference stored yet: NSE's own day change used")
    else:
        move, basis = raw, "raw vs Nifty close (basis not removed)"
        if raw is not None:
            caveats.append("GIFT move is the raw comparison with the Nifty close: the future's premium is not removed")
    voted = raw if cfg["gift_vote_basis"] == "raw" else move
    if cfg["gift_vote_basis"] == "raw":
        basis = "raw vs Nifty close (config gift_vote_basis=raw)"
    vote = sign_band(voted, cfg["gift_band_pct"]) if status == "LIVE" else None
    exp = {1: "Gap-Up", 0: "Flat", -1: "Gap-Down"}.get(vote, "Unknown")
    if prev_close is None:
        caveats.append("no previous Nifty 50 close for the raw GIFT comparison")
    return {"values": {**base, "gift_price": _r(price, 2), "gift_expiry": q.get("expiry"),
                       "gift_ref": _r(ref["price"], 2) if ref else None, "gift_ref_source": ref["source"] if ref else None,
                       "gift_vs_close_pct": _r(raw), "gift_move_pct": _r(voted), "gift_move_basis": basis,
                       "gift_vote": vote, "expected_open": exp, "gift_status": status, "gift_source": q.get("source"),
                       "gift_as_of": _ist(as_of)},
            "caveats": caveats + ([f"Nifty previous close from {pc_src}"] if pc_src and "index_levels" not in pc_src else [])}


def aggregate(us_vote, ad_vote, gift_vote, cfg: dict) -> dict:
    """The bias from the votes that exist (None = the check did not vote)."""
    votes = [v for v in (us_vote, ad_vote, gift_vote) if v is not None]
    s = sum(votes)
    n = cfg["bias_min_votes"]
    bias = "Bullish" if votes and s >= n else "Bearish" if votes and s <= -n else "Neutral"
    return {"vote_sum": s, "votes_counted": len(votes), "bias": bias}


# ── run / store ──────────────────────────────────────────────────────────────────────────────────────────────

def compute(conn, label: str = "0900", now: datetime | None = None, sources: Sources | None = None,
            cfg: dict | None = None) -> dict:
    """Run the four checks and return the row (not stored)."""
    now = (now or now_ist()).astimezone(IST)
    cfg = cfg or settings()
    src = sources or Sources()
    quotes, qerr = _call(src.dhan)
    quotes = quotes or {}
    caveats = [] if not qerr else [f"Dhan index quotes failed ({qerr})"]
    parts = {}
    for name, fn in (("us", lambda: check_us(conn, now, cfg, src)), ("vix", lambda: check_vix(conn, now, cfg, quotes)),
                     ("ad", lambda: check_ad(conn, now, cfg, src)),
                     ("gift", lambda: check_gift(conn, now, cfg, src, quotes))):
        try:
            parts[name] = fn()
        except Exception as e:                      # a bug in one check must not cost the other three
            log.error(f"  pre-open check {name}: {e}", exc_info=True)
            parts[name] = {"values": {f"{name}_status": "UNAVAILABLE", f"{name}_source": f"error: {e}"[:120]},
                           "caveats": [f"{name} check failed: {e}"[:200]]}
    row = {"date": str(now.date()), "run_label": label}
    legs = parts["us"]["values"].pop("_legs", {})
    for p in parts.values():
        row.update(p["values"])
        caveats += p["caveats"]
    row.update(aggregate(row.get("us_vote"), row.get("ad_vote"), row.get("gift_vote"), cfg))
    row["config_json"] = json.dumps({k: cfg[k] for k in ("us_band_pct", "vix_threshold", "ad_ratio_bull",
                                                         "ad_ratio_bear", "gift_band_pct", "gift_vote_basis",
                                                         "bias_min_votes")})
    row["caveats_json"] = json.dumps(caveats)
    row["fetched_at_ist"] = _ist(now)
    row["_legs"] = legs
    return row


def store(conn, row: dict):
    ensure_tables(conn)
    cols = [c for c in RUN_COLUMNS]
    conn.execute(f"INSERT INTO preopen_signals (date, run_label, {', '.join(cols)}) "
                 f"VALUES (?, ?, {', '.join('?' for _ in cols)}) ON CONFLICT(date, run_label) DO UPDATE SET "
                 + ", ".join(f"{c}=excluded.{c}" for c in cols),
                 (row["date"], row["run_label"], *[row.get(c) for c in cols]))
    conn.commit()


def run(label: str = "0900", now: datetime | None = None, conn=None, sources: Sources | None = None,
        notify: bool = False, force: bool = False) -> dict:
    """Scheduler / CLI entry: skip non-trading days, compute, store, optionally send the Telegram line."""
    from db.schema import get_connection
    from utils.trading_calendar import is_trading_day
    now = (now or now_ist()).astimezone(IST)
    cfg = settings()
    if label not in RUN_LABELS:
        raise ValueError(f"label must be one of {RUN_LABELS}")
    if not cfg["enabled"] and not force:
        return {"status": "SKIPPED", "rows": 0, "reason": "preopen_checklist.enabled is false"}
    if not is_trading_day(now.date()) and not force:
        return {"status": "SKIPPED", "rows": 0, "reason": f"{now.date()} is not an NSE trading day"}
    own = conn is None
    conn = conn or get_connection()
    try:
        ensure_tables(conn)
        row = compute(conn, label, now, sources, cfg)
        prev = latest_run(conn, row["date"])
        store(conn, row)
        try:
            evaluate(conn, today=now.date())
        except Exception as e:
            log.warning(f"  pre-open learning loop: {e}")
    finally:
        if own:
            conn.close()
    log.info(f"  🧭 Pre-open checklist {row['date']} {label}: {row['bias']} (sum {row['vote_sum']:+d} of "
             f"{row['votes_counted']}), {row.get('vix_regime')}, expected {row.get('expected_open')}")
    if notify and cfg["telegram"]:
        changed = not prev or prev.get("run_label") == "preliminary" or any(
            prev.get(k) != row.get(k) for k in ("bias", "vix_regime", "expected_open"))
        if label == "0900" or (label == "0909" and changed):
            try:
                from alerts.telegram import send_telegram
                send_telegram(telegram_text(row), category="preopen", severity="info",
                              key=f"preopen:{row['date']}:{label}")
            except Exception as e:
                log.warning(f"  pre-open Telegram line: {e}")
    return {"status": "SUCCESS" if row["votes_counted"] else "EMPTY", "rows": 1, "bias": row["bias"],
            "regime": row.get("vix_regime"), "expected_open": row.get("expected_open")}


def latest_run(conn, day=None) -> dict | None:
    """The newest run of day (default: the newest day with a run): 0909 over 0900 over preliminary."""
    ensure_tables(conn)
    if day is None:
        r = _rows(conn, "SELECT MAX(date) AS d FROM preopen_signals")
        day = r[0]["d"] if r and r[0]["d"] else None
        if day is None:
            return None
    rows = _rows(conn, "SELECT * FROM preopen_signals WHERE date=?", (str(day)[:10],))
    if not rows:
        return None
    rows.sort(key=lambda r: FINAL_ORDER.get(r["run_label"], 9))
    return _clean(rows[0])


def _clean(r: dict) -> dict:
    out = dict(r)
    out["date"] = str(out.get("date"))[:10]
    for k in ("caveats_json", "config_json"):
        try:
            out[k[:-5]] = json.loads(out.get(k) or ("[]" if k == "caveats_json" else "{}"))
        except (TypeError, ValueError):
            out[k[:-5]] = [] if k == "caveats_json" else {}
    return out


# ── GIFT reference at 15:31 ─────────────────────────────────────────────────────────────────────────────────

def capture_gift_close(conn=None, now: datetime | None = None, getter=None) -> dict:
    """15:31 IST on trading days: GIFT from the same source the morning uses, stored as global_snapshot
    (date, "close", "gift_nifty") -- the reference that removes the future's basis the next morning."""
    from db.schema import get_connection
    from research.global_sync import ensure_tables as gs_tables
    from utils.trading_calendar import is_trading_day
    now = (now or now_ist()).astimezone(IST)
    if not is_trading_day(now.date()):
        return {"status": "SKIPPED", "rows": 0, "reason": "not a trading day"}
    cfg = settings()
    q, err = _call(getter or (lambda: _gift_quote(cfg)))
    if not q:
        return {"status": "FAILED", "rows": 0, "error": err or "no GIFT quote from any configured source"}
    own = conn is None
    conn = conn or get_connection()
    try:
        gs_tables(conn)
        conn.execute("INSERT OR REPLACE INTO global_snapshot (date, label, series, price, quote_ts, captured_at) "
                     "VALUES (?,?,?,?,?,?)", (str(now.date()), "close", GIFT_SERIES, q["price"],
                                              _ist(q["as_of"]) if q.get("as_of") else None, _ist(now)))
        conn.commit()
    finally:
        if own:
            conn.close()
    return {"status": "SUCCESS", "rows": 1, "price": q["price"]}


def latest_gift(conn) -> dict | None:
    """The newest usable GIFT reading ATIP holds (a checklist run, else a 15:31 reference) for the screens that used
    to show Dhan's frozen value: {"price", "as_of", "move_pct", "basis", "source"}."""
    ensure_tables(conn)
    r = (_rows(conn, "SELECT date, run_label, gift_price, gift_as_of, gift_move_pct, gift_move_basis, gift_source, "
                     "gift_status FROM preopen_signals WHERE gift_price IS NOT NULL ORDER BY date DESC, fetched_at_ist DESC "
                     "LIMIT 1") or [None])[0]
    if r:
        return {"price": r["gift_price"], "as_of": r["gift_as_of"], "move_pct": r["gift_move_pct"],
                "basis": r["gift_move_basis"], "source": r["gift_source"], "status": r["gift_status"]}
    try:
        from research.global_sync import ensure_tables as gs_tables
        gs_tables(conn)
    except Exception:
        return None
    r = (_rows(conn, "SELECT date, price, quote_ts, captured_at FROM global_snapshot WHERE series=? AND price>0 "
                     "ORDER BY date DESC LIMIT 1", (GIFT_SERIES,)) or [None])[0]
    return {"price": r["price"], "as_of": str(r.get("quote_ts") or r.get("captured_at"))[:19], "move_pct": None,
            "basis": None, "source": "15:31 GIFT reference", "status": "LIVE"} if r else None


# ── learning loop ───────────────────────────────────────────────────────────────────────────────────────────

def _open_class(gap, band):
    v = sign_band(gap, band)
    return {1: "Gap-Up", 0: "Flat", -1: "Gap-Down"}.get(v)


def evaluate(conn=None, today: date | None = None) -> dict:
    """Fill the actual open, its class, the close and the day's direction into every unevaluated run of a finished
    or opened session. Gives up (eval_note) after EVAL_GIVE_UP_DAYS without an open."""
    from db.schema import get_connection
    own = conn is None
    conn = conn or get_connection()
    today = today or now_ist().date()
    done = waiting = 0
    try:
        ensure_tables(conn)
        for r in _rows(conn, "SELECT date, run_label, nifty_prev_close, config_json FROM preopen_signals WHERE "
                             "evaluated_at_ist IS NULL AND date<=?", (str(today),)):
            d = _d(r["date"])
            o = (_rows(conn, "SELECT nifty50 FROM index_levels WHERE date=? AND time>='09:15' AND time<='09:30:59' AND "
                             "nifty50>0 ORDER BY time LIMIT 1", (str(d),)) or [None])[0]
            c = (_rows(conn, "SELECT nifty50 FROM index_levels WHERE date=? AND time>='15:25' AND time<='15:30:59' AND "
                             "nifty50>0 ORDER BY time DESC LIMIT 1", (str(d),)) or [None])[0]
            if not c and d < today:
                c = (_rows(conn, "SELECT close AS nifty50 FROM prices_daily WHERE symbol='NIFTY50' AND date=? AND "
                                 "close>0", (str(d),)) or [None])[0]
            prev = r.get("nifty_prev_close") or nifty_prev_close(conn, d, {})[0]
            if not o or not c or not prev:
                if (today - d).days > EVAL_GIVE_UP_DAYS:
                    conn.execute("UPDATE preopen_signals SET evaluated_at_ist=?, eval_note=? WHERE date=? AND run_label=?",
                                 (_ist(now_ist()), "no open / close / previous close stored", str(d), r["run_label"]))
                else:
                    waiting += 1
                continue
            try:
                band = json.loads(r.get("config_json") or "{}").get("gift_band_pct", DEFAULTS["gift_band_pct"])
            except ValueError:
                band = DEFAULTS["gift_band_pct"]
            opn, cls = float(o["nifty50"]), float(c["nifty50"])
            gap, chg = _pct(opn, prev), _pct(cls, prev)
            conn.execute("UPDATE preopen_signals SET actual_open=?, actual_gap_pct=?, actual_open_class=?, nifty_close=?, "
                         "day_chg_pct=?, day_direction=?, evaluated_at_ist=?, eval_note=NULL WHERE date=? AND run_label=?",
                         (opn, _r(gap), _open_class(gap, band), cls, _r(chg),
                          "Up" if chg > 0 else "Down" if chg < 0 else "Flat", _ist(now_ist()), str(d), r["run_label"]))
            done += 1
        conn.commit()
    finally:
        if own:
            conn.close()
    return {"status": "SUCCESS" if done else "SKIPPED", "rows": done, "waiting": waiting}


def _hit_rate(pairs) -> dict:
    n = len(pairs)
    h = sum(1 for p in pairs if p)
    return {"n": n, "hits": h, "hit_rate_pct": round(100 * h / n, 1) if n else None}


def score_days(days: list) -> dict:
    """Hit rates over evaluated days (one row per day, the final run): each check against what it predicts --
    US and A/D and the bias against the day's close direction, GIFT's expected open against the actual open's
    class (3-way) and its sign against the gap's sign; VIX regime as the mean absolute move on Volatile vs Stable."""
    def dirn(r):
        return {"Up": 1, "Down": -1}.get(r.get("day_direction"))
    us = [r["us_vote"] == dirn(r) for r in days if r.get("us_vote") and dirn(r)]
    ad = [r["ad_vote"] == dirn(r) for r in days if r.get("ad_vote") and dirn(r)]
    ad_live = [r["ad_vote"] == dirn(r) for r in days if r.get("ad_vote") and dirn(r) and r.get("ad_basis") != "prior-day"]
    bias = [{"Bullish": 1, "Bearish": -1}[r["bias"]] == dirn(r) for r in days
            if r.get("bias") in ("Bullish", "Bearish") and dirn(r)]
    gift3 = [r["expected_open"] == r["actual_open_class"] for r in days
             if r.get("expected_open") not in (None, "Unknown") and r.get("actual_open_class")]
    gift_dir = [(r["gift_vote"] > 0) == (r["actual_gap_pct"] > 0) for r in days
                if r.get("gift_vote") and r.get("actual_gap_pct") not in (None, 0)]

    def mean_abs(rs, k):
        v = [abs(r[k]) for r in rs if r.get(k) is not None]
        return round(sum(v) / len(v), 3) if v else None
    vol = [r for r in days if r.get("vix_regime") == "Volatile"]
    stab = [r for r in days if r.get("vix_regime") == "Stable"]
    return {"days": len(days), "us": _hit_rate(us), "ad": _hit_rate(ad), "ad_preopen_only": _hit_rate(ad_live),
            "gift_open_3way": _hit_rate(gift3), "gift_direction": _hit_rate(gift_dir), "bias": _hit_rate(bias),
            "neutral_days": sum(1 for r in days if r.get("bias") == "Neutral"),
            "vix": {"volatile_days": len(vol), "stable_days": len(stab),
                    "volatile_mean_abs_day_pct": mean_abs(vol, "day_chg_pct"),
                    "stable_mean_abs_day_pct": mean_abs(stab, "day_chg_pct"),
                    "volatile_mean_abs_gap_pct": mean_abs(vol, "actual_gap_pct"),
                    "stable_mean_abs_gap_pct": mean_abs(stab, "actual_gap_pct")}}


def record(conn, windows=(20, 60)) -> dict:
    """Rolling accuracy over the last 20 / 60 evaluated trading days (the day's final run) and all of them."""
    ensure_tables(conn)
    rows = _rows(conn, "SELECT * FROM preopen_signals WHERE evaluated_at_ist IS NOT NULL AND day_direction IS NOT NULL")
    best = {}
    for r in rows:
        k = str(r["date"])[:10]
        if k not in best or FINAL_ORDER.get(r["run_label"], 9) < FINAL_ORDER.get(best[k]["run_label"], 9):
            best[k] = r
    days = [best[k] for k in sorted(best, reverse=True)]
    out = {f"last_{w}": score_days(days[:w]) for w in windows}
    out["all"] = score_days(days)
    out["first_day"] = days[-1]["date"] if days else None
    out["last_day"] = days[0]["date"] if days else None
    out["note"] = ("Each check is scored on what it predicts: US, A/D and the bias on the day's close vs the previous "
                   "close; GIFT on the open (09:15-09:30 first reading) vs the previous close; Neutral days are not "
                   "scored. A hit rate on fewer than 20 days is noise.")
    return out


def history(conn, days: int = 30) -> list:
    ensure_tables(conn)
    rows = _rows(conn, "SELECT * FROM preopen_signals ORDER BY date DESC, fetched_at_ist DESC LIMIT ?", (int(days) * 3,))
    return [_clean(r) for r in rows]


# ── text for the brief and the 09:00 line ───────────────────────────────────────────────────────────────────

def _signed(v, n=2):
    return "—" if v is None else f"{v:+.{n}f}%"


def summary_lines(row: dict) -> list:
    """Plain-text lines (no HTML) describing a run; shared by Telegram, the pre-market report and the CLI."""
    if not row:
        return ["Pre-open checklist: no run yet"]
    us = row.get("us_vote")
    lines = [f"Bias: {row.get('bias')} (votes {row.get('vote_sum', 0):+d} from {row.get('votes_counted', 0)} of 3)",
             f"Volatility: {row.get('vix_regime')}" + (f" (VIX {row['vix']:.2f} vs {row.get('vix_threshold', 15):g})"
                                                         if row.get("vix") else ""),
             f"Expected open: {row.get('expected_open')}" + (f" (GIFT {_signed(row.get('gift_move_pct'))}, "
                                                              f"{row.get('gift_move_basis')})"
                                                              if row.get("gift_move_pct") is not None else "")]
    lines.append("US: " + ({1: "green", -1: "red", 0: "mixed/flat"}.get(us, "no data")) +
                 f" (Nasdaq {_signed(row.get('nasdaq_chg_pct'))}, Dow {_signed(row.get('dow_chg_pct'))}, "
                 f"NQ {_signed(row.get('nq_chg_pct'))}, YM {_signed(row.get('ym_chg_pct'))})")
    if row.get("advances") is not None:
        lines.append(f"A/D: {row['advances']:,} / {row['declines']:,} ({row.get('ad_basis')})")
    else:
        lines.append("A/D: no data")
    if row.get("vix_regime") == "Volatile":
        lines.append(f"Advice: size x{row.get('size_multiplier')}, stops x{row.get('stop_multiplier')} wider; "
                     "prefer defined-risk, avoid option buying")
    return lines


def telegram_text(row: dict) -> str:
    from html import escape
    t = {"0900": "09:00 IST", "0909": "09:09 IST update", "preliminary": "08:15 IST preliminary"}.get(
        row.get("run_label"), "IST")
    icon = {"Bullish": "🟢", "Bearish": "🔴"}.get(row.get("bias"), "⚪")
    body = "\n".join(escape(x) for x in summary_lines(row))
    return (f"{icon} <b>ATIP — Pre-Open Checklist ({t})</b>\n{body}\n"
            f"<i>Paper only · informational, not advice · {escape(str(row.get('fetched_at_ist') or ''))} IST</i>")


def notify_payload(conn, now: datetime | None = None) -> dict:
    """The compact view the mobile apps' 09:00 notification reads (GET /api/preopen?compact=1)."""
    from utils.trading_calendar import is_trading_day
    now = (now or now_ist()).astimezone(IST)
    td = is_trading_day(now.date())
    row = latest_run(conn, now.date()) if td else None
    out = {"date": str(now.date()), "trading_day": td, "ready": bool(row), "run_label": (row or {}).get("run_label"),
           "bias": (row or {}).get("bias"), "vix_regime": (row or {}).get("vix_regime"),
           "expected_open": (row or {}).get("expected_open"), "fetched_at_ist": (row or {}).get("fetched_at_ist"),
           "notify": None}
    if row:
        vix = (f"VIX {row['vix']:.1f} ({row.get('vix_regime')})" if row.get("vix")
               else f"volatility {row.get('vix_regime')}")
        t = str(row.get("fetched_at_ist") or "")[11:16]
        out["notify"] = {"title": f"ATIP pre-open: {row.get('bias')}",
                         "body": f"{vix} · expected {row.get('expected_open')} · {t} IST"}
    return out


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="python -m strategy.preopen_checklist")
    ap.add_argument("cmd", nargs="?", default="show", choices=("run", "evaluate", "record", "show", "gift-close"))
    ap.add_argument("--label", default="0900", choices=RUN_LABELS)
    ap.add_argument("--force", action="store_true", help="run on a non-trading day / with the switch off")
    a = ap.parse_args(argv)
    from db.schema import get_connection
    if a.cmd == "run":
        print(json.dumps(run(a.label, force=a.force), indent=2, default=str))
    elif a.cmd == "gift-close":
        print(json.dumps(capture_gift_close(), indent=2, default=str))
    else:
        conn = get_connection()
        try:
            if a.cmd == "evaluate":
                print(json.dumps(evaluate(conn), indent=2, default=str))
            elif a.cmd == "record":
                print(json.dumps(record(conn), indent=2, default=str))
            else:
                r = latest_run(conn)
                print("\n".join(summary_lines(r)))
                if r:
                    print("Caveats:", "; ".join(r.get("caveats") or []) or "none")
        finally:
            conn.close()


if __name__ == "__main__":
    main()
