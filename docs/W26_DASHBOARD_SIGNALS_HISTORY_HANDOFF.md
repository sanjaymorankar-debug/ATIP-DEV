# W26: Signals table fixes + stock history view

**Status:** IMPLEMENTED BUT NOT VERIFIED. Tested in a browser against a scratch copy of the production database on a separate port. ChatGPT testing is pending.
**Branch:** `w26-dashboard-signals-history`, on top of W25 (`687b5ac`). Not merged, not deployed.

## Problems reported, and their causes

| Problem | Cause | Fix |
|---|---|---|
| Signal filter "not working" | `ft()` matched the whole row's text. Every row contains Buy and Sell order buttons, so "SELL" and "BUY" matched every row | Rows carry `data-signal`; the filter matches it exactly |
| Search "not working" | The ATIP Scores table rendered only the top 50 of about 500 scored stocks, so e.g. RELIANCE was never on the page | All scored stocks are rendered. A Show 50 / 100 / 250 / All selector keeps the default view short, applied in the current sort order, with a "Showing X of Y matching" count. Search matches symbol and company name only |
| Sorting "not working" | The sort ran but gave no visual feedback, and the 5-minute auto-refresh reset it | ▲ / ▼ on the sorted header. Sort, search, signal and Show selections persist across refreshes (sessionStorage) |
| Clicking a stock showed nothing | No stock detail view existed | New stock panel (below) |

- **Signal History:** gets the same exact signal and symbol filters, plus a count.
- **Other tab tables:** Portfolio, Top VPI, Buy Zones, RRI, MRI, CRI, FII/DII, News and Orders each get a "Filter this table…" box. It ignores the Buy/Sell buttons.

## Stock panel

Click any stock: a row in any tab, or the Trade-of-the-Day card. Clicks on buttons, inputs and the order form are not intercepted.

- **Header:** symbol, company, live price and change, with Buy / Sell buttons that open the existing order modal.
- **Key stats:** 52-week high / low; returns over 1W, 1M, 3M, 6M and 1Y; 20-day average volume.
- **Price chart:**
  - line or candles; 1M / 3M / 6M / 1Y / All; volume bars;
  - ▲ BUY / ▼ SELL markers from the signal log;
  - crosshair tooltip (OHLC, volume, ATIP score and signal for that day);
  - an ATIP score line with the 45 / 70 bands.
- **Tabs:**
  - **Score history:** every stored ATIP score with its sub-scores and signal.
  - **Signals:** the log with first target hit, best / worst move and model.
  - **Technicals:** RSI, MACD, ADX, EMAs, 200 DMA, Bollinger, pivots, plus W21 support / resistance, VWAP, Supertrend, multi-timeframe trend and beta.
  - **Position & orders:** LIVE holding, PAPER position, ATIP order rules, orders placed and W4 orders.
- **Behaviour:** Esc or the backdrop closes it. While it is open, the page's auto-refresh is held.

**API:** `GET /api/stock/{symbol}/history?sessions=400`, read-only, default `dashboard:read`. It returns 400 for a malformed symbol and 404 for one with no prices.

## Files

- `dashboard/stock_view.py` (new): the API plus the CSS, HTML and JS, kept out of the server.py f-string.
- `dashboard/server.py`:
  - the state holds all scored rows;
  - rows get data attributes; new controls;
  - the old `ft` / `hft` are replaced;
  - the assets are injected and the route registered.

## Evidence (scratch server, 501 scored stocks)

- **Table:** "reliance" finds RELIANCE; BUY returns only OLAELEC (the one BUY); WAIT returns 265 WAIT rows; Show 100 works; sort works both ways with the arrow.
- **Persistence:** a reload restored the HOLD filter, Show 100 and the descending ATIP sort.
- **Panel:**
  - Opens from ATIP Scores (WHIRLPOOL), Signal History (OLAELEC, 1 signal, marker drawn), Portfolio (IEX, holding shown), Top VPI and the Trade-of-the-Day card.
  - Row Buy buttons still open only the order modal.
  - Candles, 1M and hover all work; Esc closes.
  - A malformed symbol gets 400, an unknown one 404.
  - No server errors; the only console errors are the deliberate 400 / 404 tests.
- **Company names:** the scratch copy lacks the Dhan security-master CSV. Against production's file the lookup returns 3,395 names (OLAELEC → Ola Electric Mobility).
- **Page:** 1.0 MB instead of 0.3 MB (every stock rendered), served in 0.13 s locally.
