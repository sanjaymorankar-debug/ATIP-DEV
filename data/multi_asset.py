"""
Multi-asset data (W35: DP-21): mutual funds, commodities, currencies, bonds.

    MUTUAL FUNDS   AMFI's daily NAV file (https://www.amfiindia.com/spages/NAVAll.txt): every open-ended
                   scheme's NAV with scheme code, ISINs, AMC and category -> mf_nav. Daily, after ~23:00.
                   history_mf(scheme_code, start, end) fetches a scheme's past NAVs from AMFI's history
                   endpoint (portal.amfiindia.com DownloadNAVHistoryReport) on demand.
    COMMODITIES /  daily OHLCV via yfinance for the instruments in ASSETS -> asset_price_daily
    CURRENCIES /   (asset_class COMMODITY / FX / RATES / INDEX). yfinance prices are the global
    RATES          benchmark contracts (COMEX gold, NYMEX crude, ...) and spot FX crosses -- not MCX or
                   NSE-CDS prints: no free, documented endpoint for those exchanges' bhavcopies was
                   verified from this machine, so they are NOT claimed. assets(add=...) in config.json
                   extends the list.

    run_multi_asset(days=10)   both, daily (scheduler, 23:30)
    backfill(days=750)         the yfinance assets' history (MF history is per scheme, on demand)
    series(conn, asset_class, symbol, start, end)

The wealth track's scope exclusions (W11-W20: no MF advice) are untouched: this is market data only.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

AMFI_NAV = "https://www.amfiindia.com/spages/NAVAll.txt"
AMFI_HIST = ("https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx?mf=0&tp=1&frmdt={f}&todt={t}"
             "&schemecode={code}")
# symbol -> (asset_class, yfinance ticker, currency, name)
ASSETS = {
    "GOLD":      ("COMMODITY", "GC=F", "USD", "COMEX gold"),
    "SILVER":    ("COMMODITY", "SI=F", "USD", "COMEX silver"),
    "CRUDE_WTI": ("COMMODITY", "CL=F", "USD", "NYMEX WTI crude"),
    "BRENT":     ("COMMODITY", "BZ=F", "USD", "ICE Brent crude"),
    "NATGAS":    ("COMMODITY", "NG=F", "USD", "NYMEX natural gas"),
    "COPPER":    ("COMMODITY", "HG=F", "USD", "COMEX copper"),
    "USDINR":    ("FX", "INR=X", "INR", "USD/INR"),
    "EURINR":    ("FX", "EURINR=X", "INR", "EUR/INR"),
    "GBPINR":    ("FX", "GBPINR=X", "INR", "GBP/INR"),
    "JPYINR":    ("FX", "JPYINR=X", "INR", "JPY/INR"),
    "DXY":       ("FX", "DX-Y.NYB", "USD", "US dollar index"),
    "US10Y":     ("RATES", "^TNX", "%", "US 10-year yield"),
    "US5Y":      ("RATES", "^FVX", "%", "US 5-year yield"),
    "US3M":      ("RATES", "^IRX", "%", "US 13-week bill yield"),
}


def assets() -> dict:
    out = dict(ASSETS)
    try:
        cfg = json.loads((Path("atip_data") / "config.json").read_text(encoding="utf-8"))
        for sym, spec in ((cfg.get("multi_asset") or {}).get("add") or {}).items():
            if isinstance(spec, list) and len(spec) >= 2:
                out[sym.upper()] = (spec[0].upper(), spec[1], spec[2] if len(spec) > 2 else "", sym)
    except Exception:
        pass
    return out


# ── mutual funds ──────────────────────────────────────────────────────────
def parse_amfi(text: str) -> list:
    rows, amc, category = [], None, None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(";")
        if len(parts) >= 6 and parts[0].strip().isdigit():
            try:
                nav = float(parts[4])
                d = datetime.strptime(parts[5].strip(), "%d-%b-%Y").date()
            except ValueError:
                continue
            rows.append({"scheme_code": parts[0].strip(), "isin_growth": parts[1].strip() or None,
                         "isin_reinvest": parts[2].strip() or None, "scheme_name": parts[3].strip(), "nav": nav,
                         "date": str(d), "amc": amc, "category": category})
        elif len(parts) == 1:
            if "Schemes" in line or ("(" in line and ")" in line):
                category = line
            else:
                amc = line
    return rows


def fetch_mf(conn) -> dict:
    import requests
    r = requests.get(AMFI_NAV, timeout=60, headers={"User-Agent": "Mozilla/5.0 (ATIP research)"})
    r.raise_for_status()
    rows = parse_amfi(r.text)
    for x in rows:
        conn.execute("INSERT OR REPLACE INTO mf_nav (scheme_code,date,nav,scheme_name,isin_growth,isin_reinvest,amc,category) "
                     "VALUES (?,?,?,?,?,?,?,?)", (x["scheme_code"], x["date"], x["nav"], x["scheme_name"],
                                                  x["isin_growth"], x["isin_reinvest"], x["amc"], x["category"]))
    conn.commit()
    if rows:
        from data import lake
        d = max(x["date"] for x in rows)
        lake.write("mf_nav", d, pd.DataFrame(rows), source="amfi_navall", replace=True, conn=conn)
    return {"schemes": len(rows), "dates": sorted({x["date"] for x in rows})[-3:]}


def history_mf(conn, scheme_code: str, start, end=None) -> dict:
    import requests
    s = date.fromisoformat(str(start)[:10])
    e = date.fromisoformat(str(end)[:10]) if end else date.today()
    url = AMFI_HIST.format(f=s.strftime("%d-%b-%Y"), t=e.strftime("%d-%b-%Y"), code=scheme_code)
    r = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0 (ATIP research)"})
    r.raise_for_status()
    n = 0
    for line in r.text.splitlines():
        p = line.split(";")
        if len(p) >= 8 and p[0].strip() == str(scheme_code):
            try:
                d = datetime.strptime(p[7].strip(), "%d-%b-%Y").date()
                nav = float(p[4])
            except (ValueError, IndexError):
                continue
            conn.execute("INSERT OR IGNORE INTO mf_nav (scheme_code,date,nav,scheme_name,isin_growth,isin_reinvest) "
                         "VALUES (?,?,?,?,?,?)", (str(scheme_code), str(d), nav, p[1].strip(), p[2].strip() or None,
                                                  p[3].strip() or None))
            n += 1
    conn.commit()
    return {"scheme_code": scheme_code, "rows": n}


# ── yfinance assets ───────────────────────────────────────────────────────
def fetch_assets(conn, days=10) -> dict:
    import yfinance as yf
    spec = assets()
    tickers = [v[1] for v in spec.values()]
    data = yf.download(tickers, period=f"{max(5, int(days))}d", interval="1d", group_by="ticker", progress=False,
                       auto_adjust=False)
    n, missing = 0, []
    for sym, (cls, tk, ccy, _) in spec.items():
        try:
            df = data[tk] if len(tickers) > 1 else data
            df = df.dropna(subset=["Close"])
        except Exception:
            missing.append(sym)
            continue
        if df.empty:
            missing.append(sym)
            continue
        for idx, r in df.iterrows():
            conn.execute("INSERT OR REPLACE INTO asset_price_daily (asset_class,symbol,date,open,high,low,close,volume,"
                         "currency,source) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (cls, sym, str(idx)[:10], _f(r.get("Open")), _f(r.get("High")), _f(r.get("Low")),
                          _f(r.get("Close")), _f(r.get("Volume")), ccy, f"yfinance:{tk}"))
            n += 1
    conn.commit()
    return {"rows": n, "assets": len(spec) - len(missing), "missing": missing}


def _f(v):
    try:
        x = float(v)
        return None if x != x else x
    except (TypeError, ValueError):
        return None


def run_multi_asset(days=10, conn=None) -> dict:
    from db.schema import get_connection, log_job
    own = conn is None
    conn = conn or get_connection()
    out, errors = {}, []
    try:
        try:
            out["assets"] = fetch_assets(conn, days)
        except Exception as e:
            errors.append(f"assets: {e}")
        try:
            out["mutual_funds"] = fetch_mf(conn)
        except Exception as e:
            errors.append(f"AMFI: {e}")
        rows = (out.get("assets") or {}).get("rows", 0) + (out.get("mutual_funds") or {}).get("schemes", 0)
        status = "FAILED" if len(errors) == 2 else ("PARTIAL" if errors else "SUCCESS")
        log_job("multi_asset", status, rows, error="; ".join(errors) or None)
        return {"status": status, "rows": rows, **out, "errors": errors}
    finally:
        if own:
            conn.close()


def backfill(days=750) -> dict:
    from db.schema import get_connection
    conn = get_connection()
    try:
        return fetch_assets(conn, days)
    finally:
        conn.close()


def series(conn, asset_class: str, symbol: str, start=None, end=None) -> list:
    sql = "SELECT date, open, high, low, close, volume FROM asset_price_daily WHERE asset_class=? AND symbol=?"
    args = [asset_class.upper(), symbol.upper()]
    if start:
        sql += " AND date>=?"
        args.append(str(start)[:10])
    if end:
        sql += " AND date<=?"
        args.append(str(end)[:10])
    return [dict(zip(("date", "open", "high", "low", "close", "volume"), r)) for r in conn.execute(sql + " ORDER BY date", args)]


def catalogue(conn) -> dict:
    a = [dict(zip(("asset_class", "symbol", "first", "last", "rows"), r)) for r in conn.execute(
        "SELECT asset_class, symbol, MIN(date), MAX(date), COUNT(*) FROM asset_price_daily GROUP BY asset_class, symbol "
        "ORDER BY asset_class, symbol")]
    mf = conn.execute("SELECT COUNT(DISTINCT scheme_code), MAX(date) FROM mf_nav").fetchone()
    return {"assets": a, "mutual_funds": {"schemes": mf[0], "latest": mf[1]}}


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(description="Multi-asset data (DP-21)")
    ap.add_argument("--backfill", type=int)
    ap.add_argument("--mf-history", nargs=2, metavar=("SCHEME_CODE", "START"))
    a = ap.parse_args()
    if a.backfill:
        print(backfill(a.backfill))
    elif a.mf_history:
        from db.schema import get_connection
        c = get_connection()
        print(history_mf(c, a.mf_history[0], a.mf_history[1]))
        c.close()
    else:
        print(run_multi_asset())
