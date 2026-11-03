# ATIP design system and design-thinking notes (3 Nov 2026, prompt 10:54 PM IST)

The dashboard already had a design system: the 2 Nov redesign (shell, tokens), the 3 Nov UX audit (feed label, AX.ask,
legacy theme bridge) and UI fixes v2 (watchlist, order ticket, answers). This pass works through the brief's five
stages against that system and changes only what the stages found. The rules earlier sessions recorded are kept: one
feed label, `AX.ask` (never `alert`/`confirm`) in order flows, the legacy bridge, the money formatters, scores tabs markup.

Before / after screenshots: `docs/testing/screenshots/full-review-2026-11-03/` (`before_*` = master `29b488e`, `after_*` =
this branch, both on a copy of tonight's live database).

## 1. Empathise

One user: an active trader who reads ATIP **pre-market at 8:30 AM IST** and **monitors live 09:15–15:30**, on a Mac and
on the phone apps. Ten paper sessions (S1–S10, ₹25K each) trade by themselves. The user's four jobs:

| Job | What they need in one glance |
|---|---|
| "What do I trade today?" | Market health and Nifty first; then whether there is a BUY signal; then what each session will buy |
| "How are my 10 sessions doing?" | Ten things side by side: held / queued / planned / waiting, P&L, wins |
| "Why is this stock scored this way?" | What each score means and how it is computed, where the number is |
| "Act safely on an order" | Before anything is sent: symbol, quantity, price, value, stop-loss, PAPER or LIVE |

## 2. Define (problem statements, from the before screenshots)

1. **P1. Ten sessions are a 10-column table** (`before_sessions_light_1440.jpg`). The score column runs off the right edge at
   1,440 px, the status is plain text, and "10 of 10 filled" is said for ten *planned* picks with nothing held.
2. **P2. The score numbers do not explain themselves.** "ATIP 65 · VPI 62 · …" is a string with no colour band, no
   meaning and no formula at the number. MRI's legend text still said "Momentum now", which describes it backwards since the 3 Nov fix.
3. **P3. No single "where do we stand" line.** Market health, Nifty, sessions filled and today's P&L are on four different
   cards and pages; data freshness is only in the feed pill (HH:MM), and a price change on screen is silent.
4. **P4. Order actions are unequal.** The order modal asks; three Confirm buttons, the order ticket's Save and a basket's
   Place did not (or used the browser's `confirm()`), and a pool buy showed "BUY 100" where 100 means 100 % of the cash.
5. **P5. Light by default for a screen watched all session.** Dark followed only the OS setting.

## 3. Ideate (options per problem; the pick and why)

| Problem | Options | Picked | Why |
|---|---|---|---|
| P1 | (a) widen the table · (b) horizontal scroll · (c) a card grid with the table one click away | **(c)** | Ten cards scan in one look; each card fits a phone without scrolling sideways; the sortable table stays for comparison (`<details>Table view`) |
| P2 | (a) a legend page · (b) a tooltip per header · (c) a badge per value with colour + glyph + word + formula tooltip | **(c)** + the legend | The explanation is at the number; colour-blind safe (● ◐ ○ and the band word in the accessible name); CRI↓ shown with its arrow and a low value coloured strong |
| P3 | (a) a dashboard header on every page · (b) a KPI strip on Home + a seconds stamp in the top bar | **(b)** | The pre-market read starts on Home; the top bar stays short (the stamp gives way first when it is narrow) |
| P4 | (a) fix each flow on its own · (b) one shared `AX.confirmOrder` used by every send path | **(b)** | One dialog, one wording, one place to keep right; LIVE is the red, destructive variant |
| P5 | (a) keep "follow the device" · (b) dark default with a light toggle | **(b)** | The brief's choice for a trading screen; light and "device" stay one click away and are remembered |

## 4. Prototype (what is implemented)

### Colour tokens (defined once in `dashboard/static/atip/atip.css`, `:root` light and `[data-theme="dark"]`)

| Token | Light | Dark | Use |
|---|---|---|---|
| `--ax-bg` / `--ax-surface` / `--ax-surface-2` | #F6F7F9 / #FFFFFF / #F0F2F5 | #0B1220 / #111A2C / #17243B | page, cards, table heads |
| `--ax-text` / `--ax-text-2` / `--ax-muted` | #101828 / #344054 / #5D6B82 | #E6EBF5 / #C9D3E6 / #9AA8C1 | text levels |
| `--ax-pos` on `--ax-pos-bg` | #067647 on #E3F6EE | #6EE7B7 on #0E2A22 | up / BUY / strong score |
| `--ax-neg` on `--ax-neg-bg` | #B42318 on #FDECEA | #FCA5A5 on #3B1114 | down / SELL / weak score / LIVE |
| `--ax-warn` on `--ax-warn-bg`, `--ax-amber` | #93370D on #FEF6DA, #B54708 | #FDE68A on #2A2410, #FDB022 | caution / HOLD / neutral score / stale |
| `--ax-info` on `--ax-info-bg`, `--ax-brand` | #175CD3 on #EAF2FF, #1D4ED8 | #93C5FD on #12233F, #60A5FA | information, links, planned picks |

**WCAG AA:** all 32 text-on-background pairs pass 4.5:1 in both themes (lowest 4.81:1, light muted text on surface-2;
`.ux_build/full-review-2026-11-03/contrast.txt`). Colour is never alone: ▲/▼ on every change, ● ◐ ○ plus the band word
on every score badge, a glyph on every status chip (● Holding, ◷ Queued, ! Needs your tap, → Next pick, ○ Waiting).

### Typography and spacing
Tabular numerals (`font-variant-numeric: tabular-nums`) on every price, score, money and time; numbers right-aligned in
tables (`td.n`). Mono for the freshness stamp and ranks. Spacing scale 4 · 8 · 12 · 16 · 24 px.

### Patterns

| Pattern | Where | Notes |
|---|---|---|
| **KPI strip** | Home | Market health (score + regime) · Nifty 50 (▲/▼ %) · Sessions filled ("10 of 10", with held / queued / planned) · Today's P&L · Data freshness ("Updated HH:MM:SS IST"). Each has a tone border *and* a word |
| **Ten-session card grid** | Sessions (full) and Home (compact) | status chip, ATIP rank #k/N (CRI↓ breaks ties), why this stock, seven score badges, entry / target / stop, P&L, wins; `Table view` keeps the sortable table |
| **Score badges with formula tooltips** | session cards (and `score_glossary.badge` for any page) | `dashboard/score_glossary.FORMULAS` holds each formula in one line; the legend shows them too |
| **Sortable / filterable tables, sticky headers** | every `ax-table` | existing (`AX.enhanceTable`), unchanged |
| **Sparklines** | session cards, Home | existing (`ux_kit.spark`) |
| **Progressive disclosure** | Table view, "How the numbers work", "What do the scores mean?", the order ticket drawer, a session's own page | summary first, detail one click away |
| **Live data** | "/" CMP cells, the top bar, Home | a changed price flashes once (`AX.flash`, off under reduced motion); `[data-ax-updated]` stamps in IST, amber with ⚠ when the feed is stale; skeleton loaders (`.ax-skel`) unchanged |
| **Safety dialog** | every Confirm, the order ticket, baskets, SIP runs | `AX.confirmOrder`: symbol, side chip, quantity (≈ shares for a pool buy), price, order value, stop-loss with %, target, PAPER / LIVE; Cancel has the focus, Esc is no, LIVE is red with "Send LIVE order" |
| **Theme** | the shell's head script + `atip.js` | dark unless the viewer picked light or "device"; set before first paint (no flash) |

### Mobile apps (Android + iOS, 2026.11.03.3, build 7)
Thin native shells around these pages, so the grid, badges, KPI strip, dialog and stamp reach the phone with no store
release. Native change: both start **dark** (iOS `preferredColorScheme(.dark)`, Android `MODE_NIGHT_YES`) to match. See
`mobile/README.md`. Builds still need the SDKs (not on this Mac).

## 5. Test (the four jobs walked end to end; friction found and fixed)

Run on the "Prod" copy (tonight's database) at 1,440 px and 390 px, dark and light, in the built-in browser.

| Job | Walk | Friction found → fixed |
|---|---|---|
| What to trade today | Home → KPI strip → Trade of the Day → the ten cards | "10 of 10" read as ten positions → the tile is blue (info) until every session holds or has queued its stock, and says "0 holding · 0 queued · 10 to buy at the next pick" |
| How are my sessions doing | Sessions → cards → Table view → a session's page | The score column was cut off at 1,440 px → cards; at 390 px there is no sideways scroll (page width 390 = viewport) |
| Why this score | Hover / long-press a badge | MRI's text described it backwards → corrected; the BUY rule in the legend now states CRI < 60 |
| Act safely on an order | Order ticket on a HOLD stock; a pending Confirm | The ticket let a HOLD stock be bought → "✗ Buy only on a BUY signal", Save disabled; Confirm sent at once → the dialog (screenshot `after_order_confirm_dialog_dark.jpg`); Esc answered no (verified) |
| (layout) | `/command` with the watchlist open | The new stamp pushed the top-bar icons 49 px off-screen → it hides when the top bar is under 1,180 px (the Home tile keeps it) |

Not covered here: Safari/WebKit (Playwright's WebKit hangs on this Mac, as in the 3 Nov report), and a market-hours run
of the CMP flash (the market was closed; the flash was exercised by forcing a price change in the page).
