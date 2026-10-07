"""
Institutional / ownership data (DP-16), W27 -- all from NSE, per symbol.

    shareholding_pattern   quarterly SHP: promoter %, public %, and from the SHP XBRL the
                           mutual-fund, FPI, insurance, domestic-institution and retail %,
                           and promoter shares pledged %. (NSE corporate-share-holdings-master
                           lists each filing with its XBRL.)
    insider_trade          SEBI PIT disclosures (corporates-pit): who, buy / sell, qty, value
    sast_disclosure        SAST Reg 29 disclosures (corporate-sast-reg29)

There is no free per-stock daily MF-flow feed; MF ownership is the quarterly SHP
MF % and its change between filings, which is what SC-14 uses.

    features(conn, symbol, as_of) -> {"promoter_pct", "promoter_chg", "mf_pct", "mf_chg",
        "fpi_pct", "fpi_chg", "pledged_pct", "insider_net_cr_90d", "insider_buys_90d",
        "insider_sells_90d", "sast_promoter_net_90d"} -- only data disclosed by as_of.

run_institutional_pipeline(symbols=None, shp_xbrl=True) refreshes every tracked symbol
(incremental: SHP XBRLs already stored are not re-downloaded).
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
from datetime import date, datetime, timedelta

log = logging.getLogger(__name__)

SHP_LIST = "https://www.nseindia.com/api/corporate-share-holdings-master?index=equities&symbol={sym}"
PIT_LIST = "https://www.nseindia.com/api/corporates-pit?index=equities&symbol={sym}"
SAST_LIST = "https://www.nseindia.com/api/corporate-sast-reg29?index=equities&symbol={sym}"

SHP_CONTEXTS = {"mf_pct": "MutualFundsOrUTI_ContextI", "fpi_pct": "InstitutionsForeign_ContextI",
                "insurance_pct": "InsuranceCompanies_ContextI", "dii_pct": "InstitutionsDomestic_ContextI",
                "retail_pct": "ResidentIndividualShareholdersHoldingNominalShareCapitalUpToRsTwoLakh_ContextI",
                "promoter_x": "ShareholdingOfPromoterAndPromoterGroup_ContextI"}
PLEDGE_TAGS = ("NumberOfSharesPledgedOrOtherwiseEncumbered", "NumberOfEncumberedShares")


def _d(s):
    s = str(s or "").strip().title()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y, %H-%M", "%d-%b-%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _f(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def parse_shp_xbrl(text: str) -> dict:
    """Category percentages (as %, not fraction) and pledged % of promoter holding."""
    out = {}
    for key, ctx in SHP_CONTEXTS.items():
        m = re.search(r'<[A-Za-z0-9\-]+:ShareholdingAsAPercentageOfTotalNumberOfShares\b[^>]*contextRef="'
                      + re.escape(ctx) + r'"[^>]*>([^<]*)<', text or "")
        if m and _f(m.group(1)) is not None:
            out[key] = round(_f(m.group(1)) * 100, 3)
    prom_sh = re.search(r'<[A-Za-z0-9\-]+:NumberOfShares\b[^>]*contextRef="ShareholdingOfPromoterAndPromoterGroup_'
                        r'ContextI"[^>]*>([^<]*)<', text or "")
    for tag in PLEDGE_TAGS:
        m = re.search(r'<[A-Za-z0-9\-]+:' + tag + r'\b[^>]*contextRef="ShareholdingOfPromoterAndPromoterGroup_'
                      r'ContextI"[^>]*>([^<]*)<', text or "")
        if m and prom_sh and _f(prom_sh.group(1)):
            out["pledged_pct"] = round((_f(m.group(1)) or 0) / _f(prom_sh.group(1)) * 100, 3)
            break
    return out


def sync_shareholding(conn, symbol, nse, xbrl=True, max_xbrl=4) -> int:
    rows = nse.json(SHP_LIST.format(sym=symbol))
    if not isinstance(rows, list):
        return 0
    have = {str(r[0])[:10]: r[1] for r in conn.execute(
        "SELECT as_of, mf_pct FROM shareholding_pattern WHERE symbol=?", (symbol,))}
    n = fetched = 0
    for r in rows:
        as_of = _d(r.get("date"))
        if not as_of:
            continue
        key = as_of.date().isoformat()
        rec = {"promoter_pct": _f(r.get("pr_and_prgrp")), "public_pct": _f(r.get("public_val")),
               "submitted_at": _d(r.get("broadcastDate")) or _d(r.get("submissionDate")), "xbrl_url": r.get("xbrl")}
        need_x = xbrl and r.get("xbrl") and have.get(key) is None and fetched < max_xbrl
        if need_x:
            fetched += 1
            rec.update({k: v for k, v in parse_shp_xbrl(nse.text(r["xbrl"]) or "").items() if k != "promoter_x"})
        elif key in have:
            # keep the XBRL-derived columns already stored; refresh the listing fields
            conn.execute("UPDATE shareholding_pattern SET promoter_pct=?, public_pct=?, submitted_at=?, fetched_at=? "
                         "WHERE symbol=? AND as_of=?", (rec["promoter_pct"], rec["public_pct"], rec["submitted_at"],
                                                         datetime.now(), symbol, key))
            continue
        cols = ["promoter_pct", "public_pct", "mf_pct", "fpi_pct", "insurance_pct", "dii_pct", "retail_pct",
                "pledged_pct", "submitted_at", "xbrl_url"]
        conn.execute(f"INSERT OR REPLACE INTO shareholding_pattern (symbol,as_of,{','.join(cols)},fetched_at) "
                     f"VALUES (?,?,{','.join('?' * len(cols))},?)",
                     [symbol, key] + [rec.get(c) for c in cols] + [datetime.now()])
        n += 1
    conn.commit()
    return n


def _id(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:20]


def sync_insider(conn, symbol, nse) -> int:
    j = nse.json(PIT_LIST.format(sym=symbol))
    data = j.get("data") if isinstance(j, dict) else None
    n = 0
    for r in data or []:
        if str(r.get("secType", "")).lower() not in ("equity shares", "equity"):
            continue
        buy_q, sell_q = _f(r.get("buyQuantity")) or 0, _f(r.get("sellquantity")) or 0
        mode = str(r.get("tdpTransactionType") or r.get("acqMode") or "")
        if buy_q or sell_q:
            txn = "BUY" if buy_q >= sell_q else "SELL"
            qty, val = (buy_q, _f(r.get("buyValue"))) if txn == "BUY" else (sell_q, _f(r.get("sellValue")))
        else:
            qty, val = _f(r.get("secAcq")), _f(r.get("secVal"))
            low = mode.lower()
            txn = "SELL" if ("sell" in low or "sale" in low or "dispos" in low) else "BUY" if ("buy" in low or "acqui" in low or
                                                                              "market" in low) else "OTHER"
        did = r.get("did") or r.get("pid") or _id(symbol, r.get("acqName"), r.get("date"), qty)
        n += conn.execute(
            "INSERT OR IGNORE INTO insider_trade (disclosure_id,symbol,person,person_category,txn_type,security_type,"
            "qty,value_rs,mode,txn_from,txn_to,disclosed_at,post_pct,fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(did), symbol, r.get("acqName"), r.get("personCategory"), txn, r.get("secType"), qty, val,
             r.get("acqMode"), (_d(r.get("acqfromDt")) or datetime.min).date().isoformat() if r.get("acqfromDt") else None,
             (_d(r.get("acqtoDt")) or datetime.min).date().isoformat() if r.get("acqtoDt") else None,
             _d(r.get("date")), _f(r.get("afterAcqSharesPer")), datetime.now())).rowcount or 0
    conn.commit()
    return n


def sync_sast(conn, symbol, nse) -> int:
    j = nse.json(SAST_LIST.format(sym=symbol))
    data = j.get("data") if isinstance(j, dict) else None
    n = 0
    for r in data or []:
        did = r.get("application_no") or _id(symbol, r.get("acquirerName"), r.get("timestamp"))
        typ = str(r.get("acqSaleType") or "").upper()
        n += conn.execute(
            "INSERT OR IGNORE INTO sast_disclosure (disclosure_id,symbol,acquirer,is_promoter,txn_type,shares_acq,"
            "shares_sold,post_pct,disclosed_at,fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (str(did), symbol, r.get("acquirerName"), 1 if str(r.get("promoterType")).upper() == "Y" else 0,
             "SALE" if "SALE" in typ or "DISPOS" in typ else "ACQUISITION", _f(r.get("noOfShareAcq")),
             _f(r.get("noOfShareSale")), _f(r.get("totAftShare")), _d(r.get("timestamp")), datetime.now())
        ).rowcount or 0
    conn.commit()
    return n


def features(conn, symbol: str, as_of=None) -> dict:
    """Ownership features known by `as_of` (point in time on disclosure / submission time)."""
    as_of = str(as_of or date.today())[:10]
    out = {}
    shp = conn.execute("SELECT promoter_pct, mf_pct, fpi_pct, pledged_pct FROM shareholding_pattern WHERE symbol=? "
                       "AND (submitted_at IS NULL OR DATE(submitted_at)<=?) ORDER BY as_of DESC LIMIT 2",
                       (symbol, as_of)).fetchall()
    if shp:
        cur = shp[0]
        prev = shp[1] if len(shp) > 1 else None
        out.update(promoter_pct=cur[0], mf_pct=cur[1], fpi_pct=cur[2], pledged_pct=cur[3])
        if prev:
            for i, k in ((0, "promoter_chg"), (1, "mf_chg"), (2, "fpi_chg")):
                if cur[i] is not None and prev[i] is not None:
                    out[k] = round(cur[i] - prev[i], 3)
    since = str(date.fromisoformat(as_of) - timedelta(days=90))
    ins = conn.execute("SELECT txn_type, SUM(COALESCE(value_rs,0)), COUNT(*) FROM insider_trade WHERE symbol=? "
                       "AND DATE(disclosed_at)>? AND DATE(disclosed_at)<=? AND txn_type IN ('BUY','SELL') "
                       "GROUP BY txn_type", (symbol, since, as_of)).fetchall()
    if ins:
        buys = next((r for r in ins if r[0] == "BUY"), None)
        sells = next((r for r in ins if r[0] == "SELL"), None)
        out["insider_net_cr_90d"] = round(((buys[1] if buys else 0) - (sells[1] if sells else 0)) / 1e7, 3)
        out["insider_buys_90d"] = buys[2] if buys else 0
        out["insider_sells_90d"] = sells[2] if sells else 0
    s = conn.execute("SELECT SUM(COALESCE(shares_acq,0)) - SUM(COALESCE(shares_sold,0)), COUNT(*) FROM sast_disclosure "
                     "WHERE symbol=? AND is_promoter=1 AND DATE(disclosed_at)>? AND DATE(disclosed_at)<=?",
                     (symbol, since, as_of)).fetchone()
    if s and s[1]:
        out["sast_promoter_net_90d"] = s[0]
    return out


def run_institutional_pipeline(symbols=None, shp_xbrl: bool = True) -> dict:
    from db.schema import get_connection, log_job
    from data.nse_api import client
    conn = get_connection()
    started = datetime.now()
    try:
        if not symbols:
            from data.dhan import get_tracked_symbols
            symbols = get_tracked_symbols(conn)
        nse = client()
        tot = {"shareholding": 0, "insider": 0, "sast": 0, "failed": 0}
        for sym in symbols:
            try:
                tot["shareholding"] += sync_shareholding(conn, sym, nse, shp_xbrl)
                tot["insider"] += sync_insider(conn, sym, nse)
                tot["sast"] += sync_sast(conn, sym, nse)
            except Exception as e:
                tot["failed"] += 1
                log.warning(f"  institutional {sym}: {e}")
        rows = tot["shareholding"] + tot["insider"] + tot["sast"]
        log.info(f"  ✓ NSE institutional: {tot}")
        log_job("institutional_nse", "SUCCESS" if not tot["failed"] else "PARTIAL", rows, start_time=started)
        return {"status": "SUCCESS" if not tot["failed"] else "PARTIAL", "symbols": len(symbols), **tot}
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+")
    ap.add_argument("--no-xbrl", action="store_true")
    a = ap.parse_args()
    print(run_institutional_pipeline(a.symbols, not a.no_xbrl))
