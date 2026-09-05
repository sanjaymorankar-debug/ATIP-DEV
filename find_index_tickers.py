"""
Run this on your own machine (needs real internet access to Yahoo Finance).

    pip install yfinance
    python find_index_tickers.py

For each dead index, it tries a handful of plausible replacement tickers
and reports which ones actually return 2 days of Close data. Paste the
output back and I'll wire the confirmed-working ones into
data/markets.py's NSE_INDEXES dict for you.
"""
import yfinance as yf

CANDIDATES = {
    "midcap150":   ["^CNXMIDCAP", "^NSEMDCP50", "^CNXMDCP", "NIFTYMIDCAP150.NS", "^CRSMID"],
    "smallcap250": ["^CNXSC", "NIFTYSMLCAP250.NS", "^CNXSMALLCAP"],
    "nifty_auto":  ["^CNXAUTO", "NIFTYAUTO.NS"],
    "nifty_fmcg":  ["^CNXFMCG", "NIFTYFMCG.NS"],
    "nifty_metal": ["^CNXMETAL", "NIFTYMETAL.NS"],
    "nifty_realty":["^CNXREALTY", "NIFTYREALTY.NS"],
    "nifty_psubank":["^CNXPSUBANK", "NIFTYPSUBANK.NS", "^CNXPSU"],
    "nifty_energy":["^CNXENERGY", "NIFTYENERGY.NS"],
    "gift_nifty":  ["NIFTY_GS.NS", "GIFTNIFTY.NS"],  # heads-up: likely not on Yahoo at all
}

print(f"{'index':<15}{'ticker':<22}{'result'}")
print("-" * 55)
for idx, tickers in CANDIDATES.items():
    found_working = False
    for t in tickers:
        try:
            data = yf.download(t, period="2d", interval="1d", progress=False, auto_adjust=True)
            closes = data["Close"].dropna() if not data.empty else data
            if len(closes) >= 2:
                latest = float(closes.iloc[-1]) if hasattr(closes.iloc[-1], "__float__") else float(closes.iloc[-1].iloc[0])
                print(f"{idx:<15}{t:<22}✓ WORKS (latest close: {latest:.2f})")
                found_working = True
            else:
                print(f"{idx:<15}{t:<22}✗ no/insufficient data")
        except Exception as e:
            print(f"{idx:<15}{t:<22}✗ error: {e}")
    if not found_working:
        print(f"{idx:<15}{'':<22}  → none of the candidates worked")
    print()