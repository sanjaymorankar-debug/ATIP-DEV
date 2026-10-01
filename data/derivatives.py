"""
F&O daily summary from the NSE F&O bhavcopy (DP-08 partial, feeds SC-06), W27.

    https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{YYYYMMDD}_F_0000.csv.zip

One row per underlying per session in fo_underlying_daily:
    kind INDEX (IDF/IDO) or STOCK (STF/STO); underlying price; near-month future close,
    future OI / OI change / volume (all expiries); call and put OI, OI change and volume
    across all expiries; PCR on OI and on volume; max pain of the NEAR expiry.

The session's NIFTY PCR (OI) is also written to fii_dii_market.pcr, the column
compute_msi's Options component reads. Before W27 nothing wrote it, so the component
read a constant 1.0 every day; compute_msi now drops the component when no PCR exists
instead of assuming one.

W30 (QR-10): implied volatility from the bhavcopy's option closes (Black-Scholes, NSE
options are European; r = RISK_FREE, q = 0), on the nearest expiry at least
MIN_IV_DTE days out (expiry-week IVs are noise):
    iv_call_atm / iv_put_atm   the strike nearest the underlying
    atm_iv                     their mean (or whichever exists), in %
    iv_skew                    IV of the ~95% put minus IV of the ~105% call, vol points
    iv_expiry, iv_dte          the expiry used
Strikes with no trade and no open interest are skipped; an option price outside the
no-arbitrage bounds gives no IV (quant.derivatives.implied_vol returns None), never a
made-up one. lot_size (NewBrdLotQty) is stored for the futures short leg (QR-05).
Not built here: intraday OI, option-chain snapshots.

    run_fo_pipeline(trade_date=None, lookback=5)   fetch any of the last `lookback`
        sessions not stored yet
    python -m data.derivatives --date 2026-09-30 | --backfill 60
"""

from __future__ import annotations

import argparse
import io
import logging
import zipfile
from datetime import date, datetime, timedelta

log = logging.getLogger(__name__)

FO_URL = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{d}_F_0000.csv.zip"
PCR_INDEX = "NIFTY"
RISK_FREE = 0.065
MIN_IV_DTE = 4


def _opt_price(r):
    for k in ("ClsPric", "SttlmPric", "LastPric"):
        v = r.get(k)
        if v is not None and v == v and float(v) > 0:
            return float(v)
    return None


def _iv_block(opt, spot, trade_date):
    """IV fields for one underlying, or {} when nothing prices cleanly."""
    from datetime import date as _date
    from quant.derivatives import implied_vol
    if opt.empty or not spot:
        return {}
    exps = sorted(e for e in opt["XpryDt"].unique() if e and e != "nan")
    td = trade_date if isinstance(trade_date, _date) else _date.fromisoformat(str(trade_date)[:10])
    chosen = None
    for e in exps:
        dte = (_date.fromisoformat(str(e)[:10]) - td).days
        if dte >= MIN_IV_DTE:
            chosen, days = e, dte
            break
    if chosen is None:
        return {}
    o = opt[(opt["XpryDt"] == chosen) & ((opt["TtlTradgVol"] > 0) | (opt["OpnIntrst"] > 0))]
    if o.empty:
        return {}
    T = days / 365.0

    def iv_at(target, kind):
        side = o[o["OptnTp"] == ("CE" if kind == "call" else "PE")]
        if side.empty:
            return None
        row = side.iloc[(side["StrkPric"] - target).abs().argsort().iloc[0]]
        px = _opt_price(row)
        v = implied_vol(px, spot, float(row["StrkPric"]), T, RISK_FREE, kind) if px else None
        return round(v * 100, 2) if v else None
    c, p = iv_at(spot, "call"), iv_at(spot, "put")
    atm = round((c + p) / 2, 2) if c and p else (c or p)
    put95, call105 = iv_at(spot * 0.95, "put"), iv_at(spot * 1.05, "call")
    return {"atm_iv": atm, "iv_call_atm": c, "iv_put_atm": p,
            "iv_skew": round(put95 - call105, 2) if put95 and call105 else None,
            "iv_expiry": str(chosen)[:10], "iv_dte": days}


def download(trade_date, nse=None):
    import pandas as pd
    from data.nse_api import client
    nse = nse or client()
    raw = nse.content(FO_URL.format(d=trade_date.strftime("%Y%m%d")))
    if not raw:
        return None
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
        return pd.read_csv(z.open(z.namelist()[0]))
    except Exception as e:
        log.warning(f"  F&O bhavcopy {trade_date}: {e}")
        return None


def _max_pain(opts) -> float | None:
    """Strike minimising total option-writer payout at expiry (near expiry only)."""
    if opts.empty:
        return None
    strikes = sorted(opts["StrkPric"].dropna().unique())
    calls = opts[opts["OptnTp"] == "CE"].groupby("StrkPric")["OpnIntrst"].sum()
    puts = opts[opts["OptnTp"] == "PE"].groupby("StrkPric")["OpnIntrst"].sum()
    best, best_pay = None, None
    for s in strikes:
        pay = sum(max(0.0, s - k) * oi for k, oi in calls.items()) + sum(max(0.0, k - s) * oi for k, oi in puts.items())
        if best_pay is None or pay < best_pay:
            best, best_pay = s, pay
    return float(best) if best is not None else None


def summarise(df, trade_date) -> list:
    out = []
    df = df.copy()
    df["XpryDt"] = df["XpryDt"].astype(str)
    for sym, g in df.groupby("TckrSymb"):
        kinds = set(g["FinInstrmTp"])
        kind = "INDEX" if kinds & {"IDF", "IDO"} else "STOCK"
        fut = g[g["FinInstrmTp"].isin(["IDF", "STF"])].sort_values("XpryDt")
        opt = g[g["FinInstrmTp"].isin(["IDO", "STO"])]
        ce, pe = opt[opt["OptnTp"] == "CE"], opt[opt["OptnTp"] == "PE"]
        near = opt["XpryDt"].min() if not opt.empty else (fut["XpryDt"].min() if not fut.empty else None)
        call_oi, put_oi = float(ce["OpnIntrst"].sum()), float(pe["OpnIntrst"].sum())
        call_v, put_v = float(ce["TtlTradgVol"].sum()), float(pe["TtlTradgVol"].sum())
        und = g["UndrlygPric"].dropna()
        spot = float(und.iloc[0]) if len(und) else None
        try:
            ivb = _iv_block(opt, spot, trade_date)
        except Exception:
            ivb = {}
        lots = g["NewBrdLotQty"].dropna() if "NewBrdLotQty" in g else []
        out.append({
            "date": str(trade_date), "symbol": sym, "kind": kind,
            "underlying_price": float(und.iloc[0]) if len(und) else None,
            "fut_close": float(fut["ClsPric"].iloc[0]) if not fut.empty else None,
            "fut_oi": float(fut["OpnIntrst"].sum()) if not fut.empty else None,
            "fut_oi_chg": float(fut["ChngInOpnIntrst"].sum()) if not fut.empty else None,
            "fut_volume": float(fut["TtlTradgVol"].sum()) if not fut.empty else None,
            "call_oi": call_oi if not opt.empty else None, "put_oi": put_oi if not opt.empty else None,
            "call_oi_chg": float(ce["ChngInOpnIntrst"].sum()) if not opt.empty else None,
            "put_oi_chg": float(pe["ChngInOpnIntrst"].sum()) if not opt.empty else None,
            "call_volume": call_v if not opt.empty else None, "put_volume": put_v if not opt.empty else None,
            "pcr_oi": round(put_oi / call_oi, 4) if call_oi else None,
            "pcr_volume": round(put_v / call_v, 4) if call_v else None,
            "max_pain": _max_pain(opt[opt["XpryDt"] == near]) if near is not None and not opt.empty else None,
            "near_expiry": str(near)[:10] if near is not None else None,
            "atm_iv": ivb.get("atm_iv"), "iv_call_atm": ivb.get("iv_call_atm"), "iv_put_atm": ivb.get("iv_put_atm"),
            "iv_skew": ivb.get("iv_skew"), "iv_expiry": ivb.get("iv_expiry"), "iv_dte": ivb.get("iv_dte"),
            "lot_size": int(lots.iloc[0]) if len(lots) else None,
        })
    return out


def store(conn, rows) -> int:
    if not rows:
        return 0
    cols = list(rows[0].keys())
    conn.executemany(f"INSERT OR REPLACE INTO fo_underlying_daily ({','.join(cols)},created_at) "
                     f"VALUES ({','.join('?' * len(cols))},?)", [[r[c] for c in cols] + [datetime.now()] for r in rows])
    nifty = next((r for r in rows if r["symbol"] == PCR_INDEX), None)
    if nifty and nifty["pcr_oi"] is not None:
        d = nifty["date"]
        if conn.execute("SELECT 1 FROM fii_dii_market WHERE date=?", (d,)).fetchone():
            conn.execute("UPDATE fii_dii_market SET pcr=? WHERE date=?", (nifty["pcr_oi"], d))
        else:
            conn.execute("INSERT INTO fii_dii_market (date, pcr) VALUES (?,?)", (d, nifty["pcr_oi"]))
    conn.commit()
    return len(rows)


def get_pcr(conn, td, symbol=PCR_INDEX):
    """The session's PCR (OI) for `symbol`, or None -- never another session's value."""
    r = conn.execute("SELECT pcr_oi FROM fo_underlying_daily WHERE date=? AND symbol=?", (str(td), symbol)).fetchone()
    if r and r[0] is not None:
        return float(r[0])
    if symbol == PCR_INDEX:
        r = conn.execute("SELECT pcr FROM fii_dii_market WHERE date=? AND pcr IS NOT NULL", (str(td),)).fetchone()
        return float(r[0]) if r else None
    return None


def run_fo_pipeline(trade_date=None, lookback: int = 5, recompute: bool = False) -> dict:
    from db.schema import get_connection, log_job
    from utils.trading_calendar import is_trading_day
    conn = get_connection()
    started = datetime.now()
    try:
        end = trade_date if isinstance(trade_date, date) else (date.fromisoformat(str(trade_date)[:10])
                                                                if trade_date else date.today())
        days, d = [], end
        while len(days) < max(1, int(lookback)) and (end - d).days < lookback * 3 + 10:
            if is_trading_day(d):
                days.append(d)
            d -= timedelta(days=1)
        stored = missing = 0
        for d in days:
            if not recompute and conn.execute("SELECT 1 FROM fo_underlying_daily WHERE date=? LIMIT 1",
                                              (str(d),)).fetchone():
                continue
            df = download(d)
            if df is None or df.empty:
                missing += 1
                continue
            stored += store(conn, summarise(df, d))
        log_job("fo_bhavcopy", "SUCCESS", stored, start_time=started)
        return {"status": "SUCCESS", "rows": stored, "sessions_checked": len(days), "not_published": missing}
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--backfill", type=int, default=5)
    ap.add_argument("--recompute", action="store_true", help="re-download and re-summarise stored sessions (IV)")
    a = ap.parse_args()
    print(run_fo_pipeline(a.date, a.backfill, a.recompute))
