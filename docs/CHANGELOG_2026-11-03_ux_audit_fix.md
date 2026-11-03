# CHANGELOG: UX audit fixes, 3 Nov 2026 (IST)

Every file this change touched and why. "New" files have no `_orig/` copy in the zip. Finding IDs are from
`ATIP_UX_Audit_Findings_2026-11-03.json`. No scoring, orders engine, Dhan, pipeline or scheduler logic changed.

## Shell and design system

| File | Why |
|---|---|
| `dashboard/shell.py` | L2 SVG icon set (`icon()`); H1 feed pill in the top bar, `feed` and `alerts` in `/api/v2/shell/state`; L3 bell popover; density button; H2 the rail is the one `nav[aria-label="Main"]`, the middleware hides older pages' "Main" navs and removes "← Dashboard"; H3 adds `ax-legacy` to older pages' `<body>`; L1 `leaf_of()` and the `<title>` = breadcrumb leaf; M6 the row drawer is `inert` when closed. |
| `dashboard/static/atip/atip.css` | H3 legacy bridge (the older pages' variables and inline hex colours → `--ax-*` tokens; inline 9–11.5 px text → 12 px); icon, feed pill, popover, density and legend styles; `margin:auto` so shell dialogs centre on older pages; M4 tabular figures without a monospace face; the drawer is hidden while closed; the rail and phone bar are protected from an older page's `nav{}` rule. |
| `dashboard/static/atip/atip.js` | H7 `AX.ask()` and `AX.priceMoved()`; H1 paints the feed label wherever `[data-ax-feed]` is; L3 bell list with IST times (`AX.istShort`); density toggle; theme button tooltip; drawer `inert` on close. |
| `dashboard/ux_kit.py` | H1 `feed_label()` (live / stale > 120 s / closed / none) and `FEED_STALE_SECONDS = 120`. |
| `dashboard/score_glossary.py` (new) | H5 definitions, bands, `abbr()`, `legend_html()`, the engine's own signal rules. |

## Pages

| File | Why |
|---|---|
| `dashboard/server.py` (ATIP Scores `/`) | H4 status strip, 5 KPIs + "Global and more", collapsed sectors, 4 tab groups (ids unchanged); H5 `<abbr>` headers, legend, pill bands; M4 `money()` everywhere, ▲/▼ on changes; H6 stylesheet on tokens, ≥ 12 px; M5 order ticket (inline labels, switches, Check broker, money at risk, charges, risk meter, env); H7 no `alert`/`confirm`; L1 title; L3 one-line countdown, no reload while the ticket is open; H1 "price at HH:MM:SS IST" on CMP cells; M6 names on every control, tab panels focusable; M1 phone layout; M7 empty rows. Additive: `GET /api/orders/broker-capability`, `X-ATIP-Quotes-At` header. |
| `dashboard/stock_view.py` | H3/H6 stock panel stylesheet on tokens; the panel sits above the shell; the one `alert()` replaced by a toast. |
| `dashboard/w28_assets.py`, `dashboard/news_impact_view.py` | H3/H6 lists-tab and news-impact styles on tokens; M6 the news-impact scroll boxes are named, focusable regions. |
| `dashboard/command_page.py` (`/command`) | H1 `pFeed` and the stale banner use the shared label; H7 shell dialogs for Resume and the price-moved confirm; M7 loading / empty / error states; M1 auto-send choice wraps at 320 px. |
| `dashboard/command_more.py` (`/command/*`) | H7 shell dialogs for "Save watched rule" and "Start sessions". |
| `dashboard/command_routes.py` | H1 `_feed()` returns `feed_label()` (old keys kept). |
| `dashboard/ux_pages.py` (`/home`, `/more`) | H1 Today shows the shared feed label; M4 no "₹ ₹"; L2 icons on More. |
| `dashboard/w38_page.py` (`/m`) | H1 "Live prices" row with the shared label (`price_feed` in the summary). |
| `dashboard/w39_page.py` (`/options-builder`, `/screener`, `/signals`, `/research`, `/market-pulse`) | M2 rebuilt options builder (labels, symbol search, presets, auto-analyse, payoff chart, Greeks, max profit/loss, breakevens, tier, 3-step empty state; analysis only); M6 labels and focusable table regions; M3 no wave codes, readable valuation names; H5 screener legend and `<abbr>` headers; M1 no sideways scroll; M7 loading / empty / error; H3/H6 tokens and sizes. |
| `dashboard/w39_routes.py` | M2 additive read-only `GET /api/options/symbols`; `suitability` in the analyse response. |
| `quant/options_strategy.py` | M2 additive `TIERS` and `suitability()` (Beginner / Intermediate / Advanced). |
| `dashboard/execution_page.py` (`/trading`) | M7 per-section loading / empty / error; M1 scroll regions, no sideways scroll; M3 plain risk-limit names (key in tooltip), no wave codes; M6 labelled selects, 24 px nav links; H3/H6 tokens. |
| `dashboard/quant_page.py` (`/quant`) | M7 loading / empty / error; M3 plain data-dependency names, W40 out of the heading; M6 `#key` labelled, scroll regions; M1; H3/H6. |
| `dashboard/market_page.py` (`/market`, the shared Live-feeds block) | M6 four controls labelled, scroll regions (the Live-feeds wrapper too); M1 chart labels in HTML, not the scaled SVG; H3/H6. `live_tab_html()` unchanged in signature and ids. |
| `dashboard/ml_page.py` (`/ml`) | M6 `#monm` labelled, scroll regions; M3 W28 out of headings; M1; themed charts. |
| `dashboard/strategy_builder_page.py` | M6 all 12 condition selects and `#ek` named. |
| `dashboard/w35_page.py` (`/data-platform`) | M1 cards shrink, tables scroll in place; M6 tabs as buttons with roles, search labelled. |
| `dashboard/w34_page.py` (`/execution-lab`), `strategy_page.py`, `backtest_page.py` | M6 scroll regions and labels; H3/H6 tokens, 12 px chart text; empty rows say what to do. |
| `dashboard/enterprise_page.py` (`/account`, `/admin`, `/admin/console`) | "esc is not defined" fixed (shared script loads first); inputs labelled; read errors shown at the top. |
| `dashboard/wealth_page.py`, `dashboard/wealth_ui/*_tab.py` (7) | M6 every input and select labelled; H3/H6 paired light/dark tokens, ≥ 12 px. |
| `dashboard/global_asia.py` | H3/H6 `--ga-*` colours from tokens (follows the theme); M6 the news panel's boxes named; banner ids and function signatures unchanged. |
| `dashboard/w37_page.py` (`/brokers`), `w39_retail_page.py` (`/baskets`), `assistant_page.py` | M6 labels (tabs, file input, selects, basket legs, question box); M1 no overflow at 320. |

## Found while testing (outside the audit)

| File | Why |
|---|---|
| `enterprise/privacy.py` | The data inventory missed the 10 tables the 2 Nov sessions / accounting / reports work added (`trading_sessions`, `session_events`, `session_rules`, `session_settings`, `session_cycles`, `session_transfers`, `ledger_entries`, `recon_items`, `report_schedules`, `report_archive`), so `inventory_gaps()` and the compliance run's `data_inventory` check flagged them on the live database. Classified (financial / operational, kept). Surfaced by `test_w38_platform_compliance` during market hours, when the dashboard's order-monitor thread from an earlier test creates those tables in later tests' databases. |

## Mobile apps

| File | Why |
|---|---|
| `mobile/android/app/src/main/java/com/atip/mobile/MainActivity.kt` | H1 the native banner adds the shared feed label. |
| `mobile/android/app/build.gradle.kts` | versionCode 5, versionName 2026.11.03.1. |
| `mobile/ios/ATIP/StatusBanner.swift` | H1 the native banner adds the shared feed label. |
| `mobile/ios/project.yml` | CFBundleShortVersionString 2026.11.03.1, CFBundleVersion 5. |

## Tests

| File | Why |
|---|---|
| `tests/test_ux_audit_fix_2026_11_03.py` (new) | H1–H7, M4–M7, L1–L3 for the shell, Scores, Today, Command Center and /m. |
| `tests/test_ux_audit_fix_2026_11_03_browser.py` (new) | H7 Playwright test of the price-moved dialog with a mocked 409 (skips without Python Playwright, like the suite's other browser tests). |
| `tests/test_ux_audit_fix_2026_11_03_options.py` (new) | M2, M3, M6, H5 on the research pages; the tier rule. |
| `tests/test_ux_audit_fix_2026_11_03_legacy.py` (new) | M1, M3, M6, M7, H6 on the older pages. |
| `tests/test_w39_ui_smoke.py` | Titles now follow the breadcrumb (L1). |
| `tests/test_w39_qa_suite.py` | `/trading`'s heading lost its wave code (M3). |

## Docs and tracker

`docs/PATCH_NOTES_2026-11-03_ux_audit_fix.md`, this CHANGELOG, `docs/HOW_TO_RUN_ROLLBACK_2026-11-03_ux_audit_fix.md`,
`docs/testing/ATIP_UX_Audit_Fix_Test_Report_2026-11-03.md`, `ATIP_MASTER_TRACKER.xlsx` and `docs/ATIP_MASTER_TRACKER.csv`
(backups: `*.backup-2026-11-03-before-uxauditfix.*`).

## Not a framework change

No new library or CDN script: plain CSS and JS on the existing shell. Axe-core and Playwright were used only to test.
