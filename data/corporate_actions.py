"""
ATIP — corporate actions: one adjusted price basis for every tracked stock.

What the data showed (checked 2026-09-22):
  * Dhan's daily history is adjusted for splits and bonuses in PRICE --
    BAJFINANCE is stored at 933.1 on 2025-06-13, the session before its 1:2
    split and 4:1 bonus, where NSE's raw close was 9,331.0 -- but NOT in
    VOLUME (1,018,202 stored, NSE's raw figure), and not for dividends at all.
  * NSE's Bhavcopy files carry raw prices only; neither format adjusts its
    previous-close column.
  * The daily re-sync re-fetches only the last few days, so if Dhan re-bases a
    stock after an event only those rows move, and a false jump appears where
    the window starts.

So prices_daily holds a single basis, adjusted to today's share count, and
this module keeps it that way:
  1. sync_corporate_actions() stores NSE's corporate-action calendar.
  2. apply_corporate_actions() reconciles each event once its ex-date arrives,
     against NSE's raw close and volume for the session before it. Rows still
     on the old basis are adjusted (prices x factor; volume and delivery
     quantity / factor for share-count events); rows already adjusted are left
     alone, so nothing is ever adjusted twice.
  3. to_stored_basis() brings a bar from Dhan onto that basis before it is
     written, so a re-sync cannot undo an adjustment.
  4. entry_factor() carries a signal's entry price onto the same basis.

Dividends are not adjusted: neither Dhan nor NSE does, and nothing in ATIP
expects total-return prices.
"""
import logging
import math
import re
from datetime import date, datetime, timedelta

from db.schema import get_connection, log_job

log = logging.getLogger(__name__)

CA_URL = ("https://www.nseindia.com/api/corporates-corporateActions"
          "?index=equities&from_date={a}&to_date={b}")

# Dhan rounds adjusted prices to 2 decimals: BSE is stored at 2,332.14 where
# 6,996.5 / 3 is 2,332.17.
PRICE_TOL = 0.003
VOLUME_TOL = 0.005
SHARE_KINDS = ("SPLIT", "BONUS", "CONSOLIDATION")   # change the share count
DONE = ("adjusted", "already_adjusted")             # history is on the post-event basis
# Unknown-factor events (rights, demergers, schemes) are re-checked against
# Dhan's own adjustment for this many days after the ex-date.
RECHECK_DAYS = 14

_FV = re.compile(r"From\s+R[es]\.?\s*([\d.]+)\s*/?-?\s*(?:Per\s+Share\s+)?To\s+R[es]\.?\s*([\d.]+)", re.I)
_BONUS = re.compile(r"^Bonus\s+(\d+)\s*:\s*(\d+)$", re.I)


def classify(subject):
    """(kind, price factor) for an NSE corporate-action subject, or (None, None)
    for one that does not change the price basis (dividends, interest, AGMs,
    buy-backs). The factor multiplies prices dated before the ex-date; it is
    None when NSE's terms do not determine it (rights, demergers, schemes)."""
    s = " ".join(str(subject or "").split())
    if re.search(r"split|sub-division|consolidation", s, re.I):
        m = _FV.search(s)
        if not m:
            return "SPLIT", None
        old, new = float(m.group(1)), float(m.group(2))
        if old <= 0 or new <= 0 or old == new:
            return "SPLIT", None
        return ("SPLIT" if new < old else "CONSOLIDATION"), new / old
    m = _BONUS.match(s)
    if m:
        a, b = int(m.group(1)), int(m.group(2))   # a new shares for every b held
        return "BONUS", b / (a + b)
    if re.search(r"rights", s, re.I):
        return "RIGHTS", None
    if re.search(r"demerger", s, re.I):
        return "DEMERGER", None
    if re.search(r"arrangement|amalgamation|reduction|bonus", s, re.I):
        return "SCHEME", None       # e.g. "Scheme Of Arrangement - Bonus Ncrps 4:1"
    return None, None


def fetch_corporate_actions(from_date, to_date, session=None) -> list:
    from data.bhavcopy import get_nse_session, HEADERS
    session = session or get_nse_session()
    r = session.get(CA_URL.format(a=from_date.strftime("%d-%m-%Y"), b=to_date.strftime("%d-%m-%Y")),
                    timeout=40, headers={**HEADERS, "Accept": "application/json"})
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise ValueError(f"unexpected corporate-actions payload: {str(data)[:120]}")
    return data


def store_events(conn, events) -> int:
    """Insert the price-basis events among NSE's rows; returns how many were new."""
    from data.bhavcopy import _nse_date
    new = 0
    for e in events:
        if str(e.get("series", "")).strip() != "EQ":
            continue
        kind, factor = classify(e.get("subject"))
        ex = _nse_date(e.get("exDate"))
        sym = str(e.get("symbol", "")).strip()
        if kind is None or ex is None or not sym:
            continue
        new += conn.execute(
            "INSERT OR IGNORE INTO corporate_actions (symbol, ex_date, subject, kind, factor) "
            "VALUES (?,?,?,?,?)",
            (sym, str(ex), " ".join(str(e.get("subject")).split()), kind, factor)).rowcount
    return new


def sync_corporate_actions(trade_date=None, back=15, ahead=45, session=None) -> dict:
    """NSE's calendar from `back` days before trade_date to `ahead` days after.
    Events are announced weeks ahead, so each is stored long before its ex-date."""
    td = _as_date(trade_date) or date.today()
    try:
        events = fetch_corporate_actions(td - timedelta(days=back), td + timedelta(days=ahead), session)
    except Exception as e:
        log.warning(f"  Corporate-action calendar unavailable: {e}")
        return {"status": "FAILED", "rows": 0, "error": str(e)}
    conn = get_connection()
    try:
        new = store_events(conn, events)
        conn.commit()
    finally:
        conn.close()
    return {"status": "SUCCESS", "rows": new, "fetched": len(events)}


# ── reconciliation ────────────────────────────────────────────────────────

def _as_date(d):
    if d is None or isinstance(d, date):
        return d
    return date.fromisoformat(str(d)[:10])


def _near(a, b, tol):
    return a is not None and b is not None and b != 0 and abs(a / b - 1) <= tol


_SHARE_SQL = "MIN(kind IN ('SPLIT','BONUS','CONSOLIDATION'))"


def _later(conn, symbol, ex_date):
    """Price and volume factors of the symbol's events after ex_date that the
    stored history already carries. One factor per ex-date: BAJFINANCE's split
    and bonus are two rows sharing one combined factor."""
    p = v = 1.0
    for pf, share in conn.execute(
            f"SELECT MAX(price_factor), {_SHARE_SQL} FROM corporate_actions WHERE symbol=? "
            f"AND ex_date>? AND status IN (?,?) GROUP BY ex_date", (symbol, str(ex_date)) + DONE):
        p *= pf or 1.0
        if share:
            v *= pf or 1.0
    return p, v


def nse_reference(trade_date, symbol, session=None, _cache={}):
    """NSE's raw (close, volume) for symbol on trade_date from that day's CM
    Bhavcopy; (None, None) if the file has no such symbol (a stock since
    renamed -- TMPV was TATAMOTORS); None if the file could not be read."""
    from data.bhavcopy import download_bhavcopy_cm, get_nse_session
    key = str(trade_date)
    if key not in _cache:
        try:
            df = download_bhavcopy_cm(_as_date(trade_date), session or get_nse_session())
            df = df[df["SctySrs"] == "EQ"]
            _cache.clear()     # one session's file at a time
            _cache[key] = {r.TckrSymb: (float(r.ClsPric), float(r.TtlTradgVol)) for r in df.itertuples()}
        except Exception as e:
            log.warning(f"  No NSE reference for {trade_date}: {e}")
            return None
    return _cache[key].get(symbol, (None, None))


def _scale_rows(conn, symbol, ex_date, price=1.0, volume=1.0):
    """Multiply prices and divide volume/delivery quantity for rows before ex_date."""
    n = 0
    if price != 1.0:
        n = conn.execute(
            "UPDATE prices_daily SET open=ROUND(open*?,2), high=ROUND(high*?,2), "
            "low=ROUND(low*?,2), close=ROUND(close*?,2) WHERE symbol=? AND date<?",
            (price, price, price, price, symbol, str(ex_date))).rowcount
    m = 0
    if volume != 1.0:
        m = conn.execute(
            "UPDATE prices_daily SET volume=CAST(ROUND(volume/?) AS INTEGER), "
            "delivery_qty=CAST(ROUND(delivery_qty/?) AS INTEGER) WHERE symbol=? AND date<?",
            (volume, volume, symbol, str(ex_date))).rowcount
    return n, m


def reconcile(conn, symbol, ex_date, session=None, dhan_series=None) -> dict:
    """
    Bring the stored history before one ex-date onto the post-event basis.

    The reference is NSE's raw close and volume for the last stored session
    before the ex-date. Stored close / raw close (net of later events already
    applied) is 1 if the history is still on the old basis and equal to the
    factor if it has been adjusted; anything else is left alone and reported.
    """
    group = conn.execute("SELECT id, kind, factor FROM corporate_actions WHERE symbol=? "
                         "AND ex_date=?", (symbol, str(ex_date))).fetchall()
    ids = [r[0] for r in group]
    factors = [r[2] for r in group]
    f = math.prod(factors) if factors and all(x is not None for x in factors) else None
    share = f is not None and all(r[1] in SHARE_KINDS for r in group)

    def done(status, pf=None, prow=0, vrow=0, note=None):
        conn.executemany(
            "UPDATE corporate_actions SET status=?, price_factor=?, price_rows=?, volume_rows=?, "
            "note=?, reconciled_at=? WHERE id=?",
            [(status, pf, prow, vrow, note, datetime.now().isoformat(timespec="seconds"), i) for i in ids])
        return {"symbol": symbol, "ex_date": str(ex_date), "status": status, "price_factor": pf,
                "price_rows": prow, "volume_rows": vrow, "note": note}

    prev = conn.execute("SELECT date, close, volume FROM prices_daily WHERE symbol=? AND date<? "
                        "ORDER BY date DESC LIMIT 1", (symbol, str(ex_date))).fetchone()
    if not prev:
        return done("no_data", note="no stored bar before the ex-date")
    prev_day = str(prev[0])
    ref = nse_reference(prev_day, symbol, session)
    if not ref:
        return done("pending", note=f"no NSE reference for {prev_day} yet")
    if ref == (None, None):
        return done("no_data", note=f"{symbol} is not in NSE's file for {prev_day} (renamed since?)")
    raw_close, raw_volume = ref
    later_p, later_v = _later(conn, symbol, ex_date)
    g = prev[1] / (raw_close * later_p)      # this event's factor as the history now carries it

    if f is None:
        # Dhan applies its own factor to rights and demergers, sometimes days
        # late: take its series for the rows before the ex-date and measure it.
        # Not when an earlier event was adjusted here rather than by Dhan --
        # its series could still be on that event's old basis.
        local = conn.execute("SELECT COUNT(*) FROM corporate_actions WHERE symbol=? AND ex_date<? "
                             "AND status='adjusted'", (symbol, str(ex_date))).fetchone()[0]
        if dhan_series and prev_day in dhan_series and not local:
            for d, (o, h, l, c) in dhan_series.items():
                if d < str(ex_date):
                    conn.execute("UPDATE prices_daily SET open=?, high=?, low=?, close=? "
                                 "WHERE symbol=? AND date=?", (o, h, l, c, symbol, d))
            g = dhan_series[prev_day][3] / (raw_close * later_p)
        if _near(g, 1.0, PRICE_TOL):
            return done("unadjusted", note="NSE's terms give no factor and Dhan has not adjusted")
        return done("already_adjusted", pf=round(g, 6), note="factor measured from Dhan's history")

    if _near(g, 1.0, PRICE_TOL):
        prow, _ = _scale_rows(conn, symbol, ex_date, price=f)
        status = "adjusted"
    elif _near(g, f, PRICE_TOL):
        prow, status = 0, "already_adjusted"
    else:
        return done("unverified", note=f"stored/raw {g:.5f}, expected 1 or {f:.5f}")

    vrow, note = 0, None
    if share:
        vr = prev[2] * later_v / raw_volume if raw_volume else None
        if _near(vr, 1.0, VOLUME_TOL):
            _, vrow = _scale_rows(conn, symbol, ex_date, volume=f)
        elif not _near(vr, 1.0 / f, VOLUME_TOL):
            note = f"volume basis unclear (stored/raw {vr})"
    return done(status, pf=f, prow=prow, vrow=vrow, note=note)


def apply_corporate_actions(trade_date=None, session=None, dhan=None) -> dict:
    """Reconcile every tracked event whose ex-date has arrived, newest first per
    symbol (so each one sees the later events already applied)."""
    from data.dhan import get_tracked_symbols
    td = _as_date(trade_date) or date.today()
    conn = get_connection()
    out = []
    try:
        tracked = set(get_tracked_symbols(conn))
        recheck = str(td - timedelta(days=RECHECK_DAYS))
        todo = conn.execute(
            "SELECT DISTINCT symbol, ex_date FROM corporate_actions WHERE ex_date<=? AND "
            "(status='pending' OR (status='unadjusted' AND ex_date>=?)) "
            "ORDER BY symbol, ex_date DESC", (str(td), recheck)).fetchall()
        for sym, ex in todo:
            if sym not in tracked:
                continue
            series = None
            has_factor = conn.execute(
                "SELECT COUNT(*)=SUM(factor IS NOT NULL) FROM corporate_actions WHERE symbol=? "
                "AND ex_date=?", (sym, ex)).fetchone()[0]
            if not has_factor and str(ex) >= recheck:
                series = _dhan_series(sym, ex, dhan)
            out.append(reconcile(conn, sym, ex, session, series))
            conn.commit()
    finally:
        conn.close()
    for r in out:
        if r["status"] in ("adjusted",):
            log.info(f"  ✓ {r['symbol']} {r['ex_date']}: history before the ex-date adjusted "
                     f"x{r['price_factor']:.4f} ({r['price_rows']} price rows, {r['volume_rows']} volume rows)")
        elif r["status"] in ("unverified", "unadjusted"):
            log.warning(f"  ⚠ {r['symbol']} {r['ex_date']}: {r['status']} — {r['note']}")
    counts = {}
    for r in out:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"status": "SUCCESS", "rows": sum(r["price_rows"] + r["volume_rows"] for r in out),
            "events": counts, "detail": out}


def _dhan_series(symbol, ex_date, dhan=None):
    """{date: (open, high, low, close)} from Dhan for the year before ex_date."""
    try:
        from data.dhan import fetch_historical_daily, get_dhan_client
        if dhan is None:
            dhan, _ = get_dhan_client()
        ex = _as_date(ex_date)
        df = fetch_historical_daily(symbol, ex - timedelta(days=400), ex, dhan)
        return {str(r.date)[:10]: (r.open, r.high, r.low, r.close) for r in df.itertuples()} or None
    except Exception as e:
        log.warning(f"  Dhan history for {symbol}: {e}")
        return None


def run_corporate_actions(trade_date=None) -> dict:
    """Post-market step, before the Dhan re-sync: refresh the calendar, then
    reconcile what has reached its ex-date."""
    from data.bhavcopy import get_nse_session
    session = get_nse_session()
    cal = sync_corporate_actions(trade_date, session=session)
    res = apply_corporate_actions(trade_date, session=session)
    res["calendar"] = cal
    log_job("corporate_actions", res["status"], res["rows"], run_date=_as_date(trade_date))
    return res


# ── keeping Dhan's bars on the stored basis ───────────────────────────────

def basis_events(conn) -> dict:
    """symbol -> [(ex_date, status, price_factor, is_share_event)], one entry
    per ex-date, read once per re-sync."""
    out = {}
    for sym, ex, status, pf, share in conn.execute(
            f"SELECT symbol, ex_date, MIN(status), MAX(price_factor), {_SHARE_SQL} "
            f"FROM corporate_actions WHERE status IN ('pending','unadjusted',?,?) "
            f"GROUP BY symbol, ex_date", DONE):
        out.setdefault(sym, []).append((str(ex), status, pf, bool(share)))
    return out


def to_stored_basis(day, bar, stored, events, end_date):
    """
    A Dhan bar (open, high, low, close, volume) for `day`, on the stored basis,
    or None if it must not be written.

    None for a row before a recent ex-date that is not reconciled yet
    (pending, or a rights/demerger issue Dhan has not adjusted): those rows
    change only through reconcile(), which measures them against NSE, never a
    few at a time through the re-sync window. An older unreconciled event does
    not block -- a newly tracked stock's first full fetch must be written.
    Prices: Dhan adjusts them itself, possibly days after the ex-date, so the
    stored close decides -- the bar is scaled only if it is still on the old
    basis. Volume: Dhan never adjusts it, so it is always divided by the share
    factor, unless the stored row shows it already is.
    """
    p = v = 1.0
    end = _as_date(end_date)
    for ex, status, pf, share in events:
        if ex <= day:
            continue
        if status in ("pending", "unadjusted"):
            if str(end - timedelta(days=RECHECK_DAYS)) <= ex <= str(end):
                return None
            continue
        p *= pf or 1.0
        if share:
            v *= pf or 1.0
    o, h, l, c, vol = bar
    if p == 1.0 and v == 1.0:
        return bar
    s_close, s_vol = stored if stored else (None, None)
    if p != 1.0 and _near(c * p, s_close, PRICE_TOL) and not _near(c, s_close, PRICE_TOL):
        o, h, l, c = (round(x * p, 2) if x is not None else None for x in (o, h, l, c))
    if v != 1.0 and vol is not None and not (s_vol is not None and _near(vol, s_vol, VOLUME_TOL)
                                             and not _near(vol / v, s_vol, VOLUME_TOL)):
        vol = int(round(vol / v))
    return o, h, l, c, vol


def entry_factor(conn, symbol, signal_date) -> float:
    """What a price recorded on signal_date must be multiplied by to compare
    with the stored history today."""
    f = 1.0
    for (pf,) in conn.execute("SELECT MAX(price_factor) FROM corporate_actions WHERE symbol=? "
                              "AND ex_date>? AND status IN (?,?) GROUP BY ex_date",
                              (symbol, str(signal_date)) + DONE):
        f *= pf or 1.0
    return f
