# ATIP UI design system (updated 3 Nov 2026, UI fixes v2)

This is the current design system. It extends `docs/design/ATIP_Design_System_2026-11-02.md` (tokens, shell,
components, charts and page states as of 2 Nov); where the two differ, this file wins.

Files:

| File | What it holds |
|---|---|
| `dashboard/static/atip/atip.css` | tokens, the shell, the shared components |
| `dashboard/static/atip/atip.js` | shell behaviour, `AX.fmt` (the browser formatter), tables, drawers, and the fixes applied to older pages inside the shell |
| `dashboard/static/atip/workspace.css` / `workspace.js` | the trading workspace: watchlist rail, order ticket, chart page, depth ladder, P&L strip |
| `dashboard/static/atip/options.css` / `options.js` | the option chain card on /options-builder |
| `dashboard/fmt.py` | the server formatter (the one source of number text) |
| `dashboard/ux_kit.py` | server components (tiles, tables, line / waterfall charts) |
| `dashboard/labels.py` | plain names of jobs and the glossary of ATIP's terms |
| `dashboard/static/vendor/lightweight-charts-5.0.9/` | TradingView Lightweight Charts™ 5.0.9 (Apache-2.0, LICENSE beside it), served locally |

## 1. Tokens

Light is the default; dark follows the device unless the viewer picked a theme; `[data-theme]` on `<html>` wins.
All colours on new UI come from these tokens: `tests/test_ui_fixes_v2.py` fails if `workspace.css` holds a raw hex
colour, and the WCAG AA checks (`tests/test_ux_shell.py`, `tests/test_ui_fixes_v2.py`) cover every text token on every
surface in both themes at 4.5:1 or better.

| Token | Light | Dark | Use |
|---|---|---|---|
| `--ax-bg` / `--ax-surface` / `-2` / `-3` | #F6F7F9 / #FFFFFF / #F0F2F5 / #E4E7EC | #0B1220 / #111A2C / #17243B / #1E2D47 | page, cards, table heads, tracks |
| `--ax-border` / `-soft` | #D0D5DD / #E4E7EC | #2A3A57 / #22304A | lines |
| `--ax-text` / `-2` / `--ax-muted` | #101828 / #344054 / #5D6B82 | #E6EBF5 / #C9D3E6 / #9AA8C1 | text |
| `--ax-brand` (+ `-ink`, `-soft`) | #1D4ED8 | #60A5FA | links, primary buttons, focus, Buy |
| `--ax-pos` / `--ax-neg` (+ `-bg`) | #067647 / #B42318 | #6EE7B7 / #FCA5A5 | up / down, always with ▲ ▼ and + / − |
| `--ax-amber` **(new)** | #B54708 | #FDB022 | an inverse metric rising (India VIX), a "Check" card's word |
| `--ax-amber-line` / `--ax-flat-line` **(new)** | #F79009 / #98A2B3 | #F79009 / #667085 | the left border of warn / flat cards (decorative, not text) |
| `--ax-warn` / `--ax-info` (+ `-bg`) | #93370D / #175CD3 | #FDE68A / #93C5FD | banners, chips |
| `--ax-series-1` / `-2` | #2a78d6 / #eb6834 | #3987e5 / #d95926 | chart series, MA lines |
| `--ax-env-*`, `--ax-run*`, `--ax-kill` | as on 2 Nov | | environment badge, run pill, kill switch |

## 2. Numbers — one formatter

Every money figure, percentage, price and score goes through `dashboard/fmt.py` on the server or `AX.fmt` in the
browser. Both give the same text for the same value, and `tests/test_ui_fixes_v2.py::test_js_formatter_mirrors_python`
runs the real `atip.js` in node to prove it.

| Function (Python / JS) | Output | Rule |
|---|---|---|
| `fmt_inr(v, sign, compact)` / `AX.fmt.inr` | ₹1,23,456.78 · +₹12.50 · −₹589.74 · ₹1.25 L · ₹2.10 Cr | Indian grouping, paise; `sign` for P&L |
| `fmt_pct(v)` / `AX.fmt.pct` | +3.48% · −0.18% · 0.00% | a change of zero is "0.00%", never "+0.00%" or "−0.00%" |
| `tone_class(v, inverse)` / `AX.fmt.toneClass` | `ax-pos` · `ax-neg` · `ax-flat` · `ax-warn-t` · `ax-muted` | `inverse=True` (India VIX, risk): a rise is amber, a fall green |
| `pct_html` / `inr_html` / `AX.fmt.pctHtml` / `inrHtml` | `<span class="ax-num ax-pos">▲ +3.48%</span>` | colour **and** arrow **and** sign |
| `fmt_price(v, tick)` / `AX.fmt.price` | ₹254.10 | rounded to the NSE tick of its price band (below) |
| `fmt_score(v)` / `AX.fmt.score` | 61 | 0–100 scores are whole numbers everywhere (60.73 → 61) |
| `missing_html(why)` | grey "—" with the reason as its tooltip | missing is never ₹0.00, 0.00 %, "None", "NaN" or "₹—" |
| `live_stats(stats, ltp)` | 52-week high / low and returns moved by the live price | a 52-week high is never below the price on screen |

NSE equity tick sizes (`TICK_BANDS`, from 10 Jun 2025): below ₹250 → ₹0.01; ₹250–1,000 → ₹0.05; ₹1,000–5,000 →
₹0.10; ₹5,000–10,000 → ₹0.50; ₹10,000–20,000 → ₹1; above → ₹5.

Numbers use `tabular-nums` (`.ax-num`) and sit right-aligned in tables (`td.n`). Text is at least 12 px in tables and
14 px in body copy.

## 3. Time and freshness

- Every time is IST, written "13:50 IST" for today and "2 Nov 13:50 IST" for another day.
- Every end-of-day dataset shows an **"as of" chip**. When it is behind the trading day it should have reached, the
  chip turns amber and says "stale" (`dashboard/ux_pages.py freshness()`, `GET /api/v2/freshness`). Scores are
  expected after 18:30 IST and technical signals after their 20:30 run.
- Money says its **period**: Today, This month or Since start (`accounting/periods.py`). Home and Accounting read the
  same function, and the Accounting "P&L by period" tab checks it against the general ledger.

## 4. The shell (changes on 3 Nov)

- **Breadcrumb** (`.ax-crumbs`, a `div role=navigation` so older pages' `nav {}` rules cannot reach it): the section
  never truncates, only the page part does. The page part hides when the top bar is under 560 px, and the whole crumb
  hides only on screens 380 px or narrower.
- **Top bar** is a CSS container (`container-name: axtop`), so it responds to its own width, not the viewport's, and
  still fits when the watchlist takes room. Under 1000 px the search label hides; under 860 px the notification,
  theme and profile icons fold into **"⋯"** (`#ax-ovmenu`, `role=menu`) and the run pill shows only its dot. The bar
  never wraps.
- **Run state**: the top-bar pill is always there. The full-width banner (`#atipOpBanner`) shows only when something
  is not normal: stopped, resuming, kill switch on, LIVE money, a manual override, or a page served without the shell.
- **Watchlist toggle ☆** (key **W**), the **order ticket** (`#ax-ticket`) and the **P&L strip** (Trade screens) are
  part of the frame. Their boxes are drawn on the server, so nothing shifts when they fill.

## 5. Components (new on 3 Nov)

| Component | Class / element | Notes |
|---|---|---|
| Answer card | `.ax-answer[data-tone=good|warn|bad|flat]` with `.ax-sym` + `.ax-word` | icon + word (Yes / Check / Up / Flat / Down) + coloured left border; three per row from 900 px, stacked below |
| Segmented toggle | `.ax-seg > button[aria-pressed]` | Today / Since start, timeframes, chart type, depth levels |
| Chip | `.ax-chip` (`.warn`) | "as of", stale, ATIP score |
| Symbol chip | `a.ax-symchip` | opens `/stock/{symbol}` |
| Clamp | `[data-ax-clamp]` (`.ax-clamp`) | two lines with "…"; "Show more" appears only when the text overflows |
| Alert group | `.ax-alert` | one per kind, "· n" count, newest text, symbol chips, "n earlier" disclosure |
| Technical details | `details.ax-tech` | file names, commands, API paths and config keys, closed by default |
| How this works | `details.ax-how` | the long explanation behind a one-line summary |
| Term | `.ax-term[title]` | hover / focus definition of confluence, market gate, 75m, … (`labels.GLOSSARY`) |
| See also | `.ax-seealso` | where an older page's own link bar went |
| Watchlist | `.ax-wl` (rail) / `.ax-wl-pagewrap` (page) | rows of fixed height: symbol, LTP, ▲▼ %, signal, ATIP score; hover / focus shows B, S, chart, depth and remove |
| Order ticket | `.ax-drawer.ax-ticket` | drawer at 440 px; a bottom sheet under 768 px |
| Depth ladder | `table.ax-depth` | bid qty bar · bid · ask · ask qty bar, totals and imbalance |
| P&L strip | `.ax-pnlstrip` | Today, open positions, waiting for your tap, loss budget used |

Every control is a `<button>`, `<a>`, `<summary>` or form field. On older pages, `AX.keyboardClickables()` gives any
remaining clickable `<div>` / `<span>` / `<tr>` a role, a tab stop and Enter / Space. Focus shows a 3 px ring in
`--ax-focus`. Touch targets are at least 44 px on phones.

## 6. Older pages inside the shell (atip.js `legacy()`)

These run on every page that renders its own HTML, after load and again whenever the page repaints from its API:

1. `AX.foldLegacyNav()`: a block of four or more links to other ATIP pages, in the page's own header or side list,
   is a second navigation bar. Its links hide, and they reappear once in a **See also** row at the bottom. Status
   chips and buttons in that bar stay.
2. `AX.foldTech()`: developer text in a sentence (`python -m …`, `*.py`, `config.json …`, `POST /api/…`) moves into one
   **Technical details** disclosure at the end of its paragraph.
3. `AX.keyboardClickables()`: as above.
4. `AX.wrapWide()`: a wide table scrolls inside its own box, so the page never scrolls sideways at 360 px. Long
   unbroken strings wrap (`overflow-wrap: break-word`).
5. `AX.terms()`: glossary tooltips on table headers and labels.

## 7. Charts

- **Pro chart** (`/stock/{symbol}`): Lightweight Charts 5.0.9, colours read from the tokens on each draw (so the theme
  carries over).
  - Panes: candles (or line), volume, RSI 14 and MACD 12-26-9 below.
  - Overlays: MA 20 / 50 / 200 and Bollinger 20, 2.
  - Marks: ATIP BUY / SELL markers, and price lines for active buy rules, stops and targets.
  - Interaction: crosshair with an OHLC + % + volume legend.
  - Timeframes: 1D / 5D (15-minute bars) and 1M–All (daily).
  - The last bar follows the live price every 15 s.
- **Stock modal** (the classic view on `/`): pattern labels are placed collision-aware, inside the plot and clear of
  the live price's level, with a halo.
- **Small charts** stay plain SVG (`ux_kit.line_chart`, `waterfall`, `spark`). A row with no data shows a grey "—"
  with a tooltip explaining why.

## 8. Page states

- Every **empty state** says why and what happens next ("No option chain stored for X: the F&O bhavcopy job fills it
  after 18:30 IST").
- Every **stale state** carries an "as of <IST time>" chip.
- Every **missing value** is a grey "—" with the reason in its tooltip.
- The automated DOM scan (`python -m tools.ui_review scan`, `tests/test_ui_fixes_v2_browser.py`) fails a page that
  shows any of the following:
  - a raw float beyond 2 decimals;
  - None, NaN or undefined;
  - "₹—";
  - an escaped entity;
  - a red 0.00 %;
  - developer text in user copy;
  - a second navigation bar;
  - a clickable element that is not keyboard reachable;
  - horizontal page scroll at 360 px.
