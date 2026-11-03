**Date of prompt:** 3 Nov 2026 Time 08:13 AM > (work 08:18–11:45 IST; every time below is IST)

# ATIP delta patch: the remaining UX audit findings (H1–H7, M1–M7, L1–L3), 3 Nov 2026

Source: `ATIP_UX_Audit_Fix_Prompt_2026-11-03.md` and `ATIP_UX_Audit_Findings_2026-11-03.json` (the 3 Nov Step 1 audit), plus the
07:03 IST design-pack implementation prompt (tokens, theme remembered, tabular figures, WCAG 2.2 AA, the approved innovations).
Branch `claude/ux-audit-fix-2026-11-03`. View layer only: no scoring, orders engine, Dhan, pipeline or scheduler logic changed.
PAPER throughout; `atip_data/config.json`, secrets and the live `atip_data/atip.db` were never opened for writing (tests ran on a
copy of the database).

C1–C4 (fixed earlier on 3 Nov, `docs/PATCH_NOTES_2026-11-03_audit_fixes.md`) are untouched: Buy only on BUY, PAPER/LIVE in the
order ticket, one trade-plan formula, one kill switch. `tests/test_tod_buy_rules_2026_11_03.py` stays green.

## What changed, per finding

| ID | Fix | Where |
|---|---|---|
| H1 | One helper, `ux_kit.feed_label(conn, market_open)` → `{state: live / stale / closed / none, text, short, age_s}`, used by Today, the shell top bar (new feed pill), Command Center (`pFeed` + banner), `/m` and both phone apps' native banners. Outside market hours it says "Market closed · last tick HH:MM IST", never "live". Stale = a tick older than 120 s in market hours (Command Center used 60 s before). Every CMP cell the 15 s poller updates now has the tooltip "price at HH:MM:SS IST" (new response header `X-ATIP-Quotes-At` on `/api/live-quotes`; the JSON body is unchanged). | `dashboard/ux_kit.py`, `command_routes.py`, `command_page.py`, `ux_pages.py`, `w38_page.py`, `shell.py`, `atip.js`, `server.py`, mobile banners |
| H7 | No `alert()` / `confirm()` left in `server.py` or `command_*.py`. The shell gained `AX.ask()` (its `<dialog>`, safe answer focused first, Esc = no) and `AX.priceMoved()`: trigger price, price now, the move in ₹ and %, "against you", and two buttons "Place at ₹X" / "Cancel". Validation messages appear inside the order ticket (and as a toast); successes and failures as toasts. Without the shell (`?shell=0`) nothing is placed: the forced confirm needs the dialog. | `atip.js`, `server.py`, `command_page.py`, `command_more.py`, `stock_view.py` |
| M5 | Order ticket: checkbox labels hold their box; the two high-impact settings are switches (`role="switch"`) with an explanation line; a warning appears when entry confirmation is off; the `python -m orders.rules --probe-broker` text is now plain words plus a **Check broker** button (new read-only route `GET /api/orders/broker-capability`, which only inspects the installed Dhan SDK); the preview adds order value, **money at risk** (qty × (entry − stop) + charges), **estimated charges** from `backtest/costs.py` (delivery or intraday), a **risk meter** and where the order goes (PAPER/LIVE). The ticket sits above the shell bars; the page no longer auto-refreshes while it or a dialog is open. | `server.py` |
| M4 | One formatter: `ux_kit.inr` (Indian grouping, 2 decimals) in Python (`server.money()`) and its JS twin `inrf()`: ₹9945.5 → ₹9,945.50, 1231900 → ₹12,31,900.00. Today's money card no longer prints "₹ ₹0.00" (the symbol is ▲ / ▼ / •). Price changes carry ▲ / ▼ as well as colour. Numbers use tabular figures in the text face instead of a monospace face. | `server.py`, `ux_pages.py`, `ux_kit.py`, `atip.css` |
| H2 | The shell rail is the one `<nav aria-label="Main">` (the phone bar is "Sections"). Inside the shell the 17-link bar on `/` is hidden (kept for `?shell=0`), Command Center's own left nav is hidden (its items are already rail sub-items), and every "← Dashboard" link is removed by the shell middleware. An older page's own `nav { … }` rule can no longer reshape the rail or the phone bar. | `shell.py`, `atip.css`, `server.py` |
| H3 | Legacy bridge in `atip.css`: on every older page (`body.ax-legacy`, set by the middleware) the variables those pages share (`--bg --panel --line --text --muted --accent --good --bad --warn …`) point at the shell's light/dark tokens, and their inline hex colours (`#64748b`, `#94a3b8`, `#0f172a`, `#1e293b`, …) map to tokens too. The Scores page's own stylesheet, the stock panel, the lists tab and the news-impact panel now use `var(--ax-…, old hex)`. The other older pages (Trading, Quant, Market, ML, Strategies, Strategy builder, Backtests, Execution lab, Data platform, Account/Admin, Wealth, Global & Asia, Brokers, Baskets, Assistant) and the five research pages (Options builder, Screener, Signals, Research, Market pulse) also had their hard-coded colours replaced by the bridged variables or `var(--ax-…, old hex)`; status pills set both background and text from paired tokens. | `atip.css`, `shell.py`, `server.py`, `stock_view.py`, `w28_assets.py`, `news_impact_view.py` |
| L1 | `<title>` = the breadcrumb's last part on every page ("ATIP Scores · ATIP" on `/`); the middleware rewrites older pages' titles; `/home` stays Home, `/` keeps working. | `shell.py`, `server.py` |
| L2 | Inline SVG icon set (24 px grid, `currentColor`, `aria-hidden`) for the rail, the phone bar, menu, search, bell, density, theme and profile; labels stay text. | `shell.py`, `ux_pages.py`, `atip.css` |
| H4 | Scores page: one status strip (most severe item first, "+N more" for the rest; the run state is already in the top bar) with "Data as of … · Refresh in m:ss · Refresh now"; 5 KPI tiles (Market health, Nifty 50, India VIX, GIFT Nifty, Sentiment) with Bank Nifty, S&P 500, Gold, USD/INR and the old index/global sidebar in a "Global and more" panel; sector rotation collapsed to one line naming the strongest and weakest sector (tiles on "show all", with ▲/▼); the 13 tables grouped as **Scores** (ATIP Scores, Portfolio) · **Lists** (VPI, Buy Zones, RRI, MRI, CRI) · **Flows & News** (FII/DII, News, Global & Asia) · **Activity** (Signal History, Orders, Live). Tab ids, labels and order are unchanged; tabs are keyboard-usable (`role=tab`, Enter/Space). | `server.py` |
| H5 | `dashboard/score_glossary.py`: one-line definitions, the real colour bands (pills: 70+ strong, 45–69 neutral, below 45 weak; CRI the other way round) and the engine's own signal rules (read from `scores/engine.py`). Every score header on `/` and the Top-25 tabs is an `<abbr title>`; a keyboard-reachable "What do the scores mean?" legend sits above the table; each pill names its band. The Screener has the same legend (`legend_html("screenerLegend")`) and `<abbr>` score headers. | `score_glossary.py`, `server.py` |
| H6 | Nothing under 12 px on the Scores page (tables 12.5 px, body 13.5 px); muted text uses `--ax-muted` (≥ 4.5:1 in both themes). The same minimums on every older page (12 px labels, 12.5 px tables, 13 px body), tabular figures. | `server.py`, `atip.css`, `stock_view.py` |
| M6 | The shell's row drawer is `inert` and hidden while closed (it was `aria-hidden` over focusable buttons: 20 axe failures, one per page); every select/input on `/` and the order ticket has a name; the Scores tab panels are focusable regions. On every other audited page: every select, input and textarea named (Strategy builder's 12 condition selects, Market 3, Trading 2, Quant 1, the Options builder's fields, Screener and Signals), every scrolling table a `tabindex="0" role="region" aria-label` region (Trading, Market, Quant, Data platform, ML, Strategies, Backtests, Execution lab, Global & Asia, the news-impact panel), tabs as real buttons with tab roles, nav links at least 24 px tall. | `shell.py`, `atip.js`, `atip.css`, `server.py` |
| M7 | Command Center: shimmer rows while loading, one-sentence empty states (why + what next) and an error state that says it retries every 15 s, for Waiting for you, Top signals, Sessions and Recent activity. Empty states on the Scores table and the VPI / Buy-zone / CRI lists. Trading and Quant (and the research pages): every table has a shimmer while loading, one sentence when empty (why and what to do, e.g. decisions arrive with the 16:45 IST post-market run) and an error box with what to try; each section loads on its own, so one failed read no longer blanks the page. | `command_page.py`, `server.py` |
| M1 | Scores page at 390 / 320 px: the KPI row, strip and filter bar wrap, the table scrolls inside its own focusable region with the symbol column sticky; Command Center's auto-send choice wraps at 320 px. Account, Data platform, Market, ML, Quant and Trading (and Baskets) no longer scroll sideways at 390 or 320 px: tables scroll inside their own region with a sticky first column, header links wrap, chart labels moved out of the scaled SVG. The "esc is not defined" error on Account, Admin and Admin console is fixed (the shared script now loads before the page script). | `server.py`, `command_page.py` |
| M2 | Options builder: every field labelled; the underlying is a search box over the 226 symbols with a stored chain or F&O bhavcopy (new read-only `GET /api/options/symbols`); presets Long call, Long put, Bull call spread, Bear put spread, Iron condor (+ every other template); leg edits re-analyse after 300 ms with no Analyse press; a themed payoff chart (zero line, breakevens, profit/loss shading, a text summary and table view), net Greeks (Δ Γ Θ ν), max profit / max loss ("Unlimited" when flagged), breakevens, probability of profit, reward to risk, and the suitability tier with its reason (`quant/options_strategy.suitability`: Beginner = only bought options; Intermediate = every sold leg covered by a bought one that lives as long, e.g. spreads and iron condor; Advanced = anything uncovered or unlimited loss). Empty state explains the 3 steps. Still analysis only: no order button. Checked: NIFTY iron condor (30 Oct bhavcopy, expiry 9 Nov) shows max loss −₹1,573.00, max profit ₹4,927.00, breakevens 22,874 and 23,226, the Greeks and tier Intermediate without pressing Analyse. | `w39_page.py`, `w39_routes.py`, `quant/options_strategy.py` |
| M3 | No wave code (W4, W6, W25, W28, W36, W39, W40 …) in any `<title>`, `<h1>`–`<h3>` or page subtitle. Trading's risk-limit table shows plain names ("Max position size, % of equity") with the config key in a tooltip; Quant's data-dependency keys and the options valuation methods read as words; Market pulse results read as a sentence (the JSON is in the tooltip). Some status text from the API still names a wave ("refuses every call in W4"); none of it is a heading. | `execution_page.py`, `quant_page.py`, `w39_page.py`, `ml_page.py` and the other older pages |
| L3 | The bell opens a popover of the latest 5 alerts with IST times ("2 Nov 23:37 IST") and a link to all alerts; the countdown reads "Refresh in 4:59" on one line in the status strip. | `shell.py`, `atip.js`, `atip.css`, `server.py` |

Design-pack innovations (07:03 prompt) added where they fit these fixes: the score explain panel (H5 legend + band in every
pill), the sector heatmap (themed, ▲/▼), a **density toggle** (compact rows, remembered per browser like the theme), the order
ticket's **risk meter**, and the options builder's **tier-aware** analysis. Ctrl K and the keyboard shortcuts already existed.

## New API surface (additive, flagged)

- `GET /api/orders/broker-capability`: what the installed `dhanhq` SDK offers for broker-side stops. Reads the local package only.
- `X-ATIP-Quotes-At` response header on `GET /api/live-quotes` (HH:MM:SS IST of the fetch). Body unchanged.
- `GET /api/v2/shell/state` gained `feed` (the H1 label) and `alerts` (latest 5).
- `GET /api/options/symbols` (read-only) and a `suitability` field on `POST /api/options/analyse`.

## Found while testing (outside the audit)

`enterprise/privacy.py`: the privacy data inventory did not classify the 10 tables added on 2 Nov (sessions, ledger,
reconciliation, report schedules and archive). On the live database `inventory_gaps()` listed all 10, so the compliance run's
`data_inventory` check would fail. They are now classified (financial or operational, kept). No data is changed.

## Mobile apps

Both apps are WebView shells and pick the web changes up from the server. Native change: the status banner now adds the H1 feed
label from `/api/v2/shell/state` (`feed.text`), in the web pages' own words. Android `versionCode 5` / `versionName 2026.11.03.1`;
iOS `CFBundleShortVersionString 2026.11.03.1` / `CFBundleVersion 5`. Builds: **BLOCKED** on this Mac (no JDK, Android SDK or Gradle;
only Xcode Command Line Tools, no full Xcode, no `xcodegen`). The Swift change passes `swiftc -parse`.

## Apply and roll back

Unzip `deliverables/2026-11-03/ATIP_ux_audit_fix_delta_2026-11-03.zip` over `~/ATIP`, then restart the dashboard (on the Mac:
check `git status` for other sessions' work, `.venv/bin/python -m ops backup --kind pre-release`, then
`launchctl kickstart -k gui/$UID/com.atip.platform`). Roll back: copy the zip's `_orig/` files back over the same paths and restart.
`ATIP_ux_audit_fix_2026-11-03.diff` is the unified diff of the same change. The `_orig/` copies are the working tree as it stood at
08:18 IST, which already held the uncommitted C1–C4 fixes, the 3 Nov tax change and the Live-tab app change.

## Owner decision, 3 Nov 2026 ~11:15 IST: no Trade of the Day without a BUY signal

The owner answered the audit's open question (6.5): "if there is no buy signal it is be blank or can say no buy signal for
today". The engine's pick (`ai_scores.is_tod`) is unchanged; every place that shows it now shows it only when its signal is BUY,
and otherwise says **"No BUY signal for today"**:
- ATIP Scores: the card says so, with how the day's scores split (e.g. "0 BUY · 327 HOLD · 172 WAIT · 0 SELL") and what to do;
  the ⭐ in the table marks the pick only when it is a BUY.
- Today, `/m`, the Telegram Trade of the Day alert, the morning digest's line and the pre-market report's `trade_of_day`.
- The Telegram alert and the digest now use the dashboard's one trade-plan formula (`compute_trade_plan`, C3) for a BUY pick;
  they still used close ± k×ATR. `/api/tod` (a data endpoint with a contract test) still returns the engine's pick as stored.

## Found on the live Mac after the 11:27 restart: the feed label

Stored quote snapshots land every 15 minutes, so a label read from them alone said "Feed stale" most of the trading day while
the prices on screen were seconds old. `feed_label()` now takes the newest of the snapshots, a ticking feed in
`live_feed_status` (the index WebSocket) and the dashboard's own 15 s quote fetch. Live at 11:29:51 IST: "Live · last tick 12 s ago".

## Committed and deployed

- `7cd1dec` (11:22 IST, made by the redesign session at the owner's request): this work together with the C1–C4 fixes and the
  tax-law + Live-tab work, on branch `claude/ux-audit-fix-2026-11-03`. Its message says no pipeline logic changed; strictly,
  `alerts/telegram.py` and `pipeline/premarket_report.py` carry the display-only Trade of the Day rule above.
- `f3718ec` (11:29 IST): the feed-label fix.
- Not merged into master, not pushed.
- Live Mac: `ops backup --kind pre-release` at 11:26 IST (`atip_data/backups/atip-pre-release-20261103-112652.db`), restarted
  with `launchctl kickstart` at 11:27:06 IST and again at 11:29:48 IST for the feed fix (pid 29396). Checked at 11:29:51 IST:
  every page 200, PAPER, running, not halted, 10 sessions running, feed live.
