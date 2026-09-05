"""
ATIP — Symbol → Company Name Lookup
====================================
Purely additive helper. Does NOT touch the Dhan download/scoring pipeline.

It reads the security master CSV that data/dhan.py already downloads
(atip_data/raw/dhan/security_id_list.csv) and builds a {SYMBOL: "Company Name"}
map so the dashboard can show a readable company name under every ticker.

Safe by design:
  - Never raises. Any failure just returns {} / "" and the dashboard falls
    back to showing the bare symbol, exactly like before.
  - Cached in memory per process so this doesn't re-parse the CSV on every
    dashboard request.

Usage:
    from data.companies import load_company_names, get_company_name
    names = load_company_names()          # {"RELIANCE": "Reliance Industries Ltd", ...}
    get_company_name("TCS")               # "Tata Consultancy Services Ltd" or "" if unknown
"""
import logging
from pathlib import Path

log = logging.getLogger(__name__)

SEC_LIST_PATH = Path("atip_data/raw/dhan/security_id_list.csv")

# Candidate column names (Dhan's CSV headers vary slightly by mode/version).
# Ordered by preference — first match wins.
_SYMBOL_COL_CANDIDATES = ["SEM_TRADING_SYMBOL", "TRADING_SYMBOL", "SYMBOL"]
_NAME_COL_CANDIDATES = [
    "SEM_CUSTOM_SYMBOL", "SM_SYMBOL_NAME", "SEM_INSTRUMENT_NAME",
    "COMPANY_NAME", "SECURITY_NAME", "SYMBOL_NAME", "NAME",
]
_SERIES_COL_CANDIDATES = ["SEM_SERIES", "SERIES"]
_EXCH_COL_CANDIDATES = ["SEM_EXM_EXCH_ID", "EXCHANGE", "EXCH"]

_NAME_MAP: dict = {}
_LOADED = False


def _find_col(columns, candidates):
    up = {c.upper(): c for c in columns}
    for cand in candidates:
        if cand in up:
            return up[cand]
    # loose fallback: substring match
    for c in columns:
        cu = c.upper()
        for cand in candidates:
            if cand in cu:
                return c
    return None


def load_company_names(force_reload: bool = False) -> dict:
    """Return {SYMBOL: company_name} built from the Dhan security master CSV.
    Cached after first successful load. Never raises."""
    global _NAME_MAP, _LOADED
    if _LOADED and not force_reload:
        return _NAME_MAP

    _NAME_MAP = {}
    try:
        import pandas as pd
        if not SEC_LIST_PATH.exists():
            log.info("companies: security_id_list.csv not found yet — no company names to show")
            _LOADED = True
            return _NAME_MAP

        df = pd.read_csv(str(SEC_LIST_PATH))
        sym_col = _find_col(df.columns, _SYMBOL_COL_CANDIDATES)
        name_col = _find_col(df.columns, _NAME_COL_CANDIDATES)
        series_col = _find_col(df.columns, _SERIES_COL_CANDIDATES)
        exch_col = _find_col(df.columns, _EXCH_COL_CANDIDATES)

        if not sym_col or not name_col:
            log.warning(f"companies: couldn't find symbol/name columns in {list(df.columns)[:12]}")
            _LOADED = True
            return _NAME_MAP

        mask = None
        if series_col is not None:
            mask = df[series_col].isin(["EQ", "BE", "SM", "N"])
        if exch_col is not None:
            exch_mask = df[exch_col].astype(str).str.contains("NSE", na=False)
            mask = exch_mask if mask is None else (mask & exch_mask)
        df_f = df[mask] if mask is not None else df

        name_map = {}
        for _, row in df_f.iterrows():
            sym = str(row[sym_col]).strip().upper()
            name = str(row[name_col]).strip()
            if sym and name and name.lower() != "nan":
                name_map[sym] = name
        _NAME_MAP = name_map
        log.info(f"companies: loaded {len(_NAME_MAP)} symbol → company name mappings")
    except Exception as e:
        log.warning(f"companies: failed to load company names ({e}) — dashboard will show symbols only")
        _NAME_MAP = {}
    finally:
        _LOADED = True
    return _NAME_MAP


def get_company_name(symbol: str) -> str:
    """Best-effort company name for a symbol. Returns '' if unknown."""
    if not symbol:
        return ""
    names = load_company_names()
    sym = symbol.upper().strip()
    if sym in names:
        return names[sym]
    for suffix in ["-EQ", "-BE"]:
        if sym.endswith(suffix) and sym[: -len(suffix)] in names:
            return names[sym[: -len(suffix)]]
    return ""


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    m = load_company_names(force_reload=True)
    print(f"Loaded {len(m)} company names.")
    for sym in list(m)[:10]:
        print(f"  {sym:<15} {m[sym]}")
