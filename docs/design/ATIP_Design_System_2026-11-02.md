# ATIP design system (2 Nov 2026)

One stylesheet and one script, served from `/static/atip/`, used by every page:

- `dashboard/static/atip/atip.css`: the tokens, the shell and the components.
- `dashboard/static/atip/atip.js`: the shell's behaviour, the table behaviours, drawers, toasts and chart hover.
- Server-side components live in `dashboard/ux_kit.py`.

Every class starts with `ax-`, so the stylesheet cannot restyle the older pages it is added to. The only rule that touches them is `body.ax-shelled`, which makes room for the rail and the tab bar.

## 1. Tokens

Light is the default. Dark is used when the device asks for it, unless the viewer picked light. The ◐ button stores `light`, `dark` or `auto` in `localStorage["atip-theme"]`, and `?theme=` overrides it. `[data-theme]` on `<html>` wins in both directions.

| Token | Light | Dark | Use |
|---|---|---|---|
| `--ax-bg` | #F6F7F9 | #0B1220 | page |
| `--ax-surface` / `-2` / `-3` | #FFFFFF / #F0F2F5 / #E4E7EC | #111A2C / #17243B / #1E2D47 | cards, table heads, tracks |
| `--ax-border` / `-soft` | #D0D5DD / #E4E7EC | #2A3A57 / #22304A | lines |
| `--ax-text` / `-2` / `--ax-muted` | #101828 / #344054 / #5D6B82 | #E6EBF5 / #C9D3E6 / #9AA8C1 | text, ≥ 4.5:1 on every surface |
| `--ax-brand` (+ `-ink`, `-soft`) | #1D4ED8 | #60A5FA | links, primary buttons, focus |
| `--ax-pos` / `--ax-neg` (+ `-bg`) | #067647 / #B42318 | #6EE7B7 / #FCA5A5 | gains and losses, always with + / − |
| `--ax-warn` / `--ax-info` (+ `-bg`) | #93370D / #175CD3 | #FDE68A / #93C5FD | banners, status pills |
| `--ax-series-1` / `-2` | #2a78d6 / #eb6834 | #3987e5 / #d95926 | charts: actual / gross; totals / deductions |
| `--ax-env-paper` / `-sandbox` / `-live` | #0E7490 / #6941C6 / #B42318 | same | environment badge, white text |
| `--ax-run` / `-stop` / `-resume` (+ `-ink`) | #064E3B / #7C2D12 / #1E3A8A | same | run state, same colours as the native apps |

`tests/test_ux_shell.py::test_design_tokens_meet_wcag_aa_contrast_in_both_themes` checks every text-on-surface pair at 4.5:1 or better in both themes.

Type is the system UI font at 14 px/1.5. Numbers use the monospace stack with tabular figures.

Spacing follows a 4 px base: 6, 8, 10, 12, 14, 16, 20.

Radii are 6 px and 10 px. Touch targets are at least 38–44 px.

## 2. Money and time

- **₹ with Indian grouping, to the paisa**, for example ₹1,25,000.00. Python uses `enterprise.session_reports.inr`; JavaScript uses `AX.inr`.
- **P&L always carries a sign as well as a colour**: +₹750.00 / −₹72.27. Colour is never the only cue.
- **Rates and shares are unsigned and neutral**: a `rate` cell, such as STCG 20.00%.
- **Gross and actual are never one number.** They sit side by side, labelled "Gross P&L (before any cost)" and "Actual P&L (after charges and estimated tax)". Tax always says "est.".
- **Every report figure carries its env**, VIRTUAL or ACTUAL. Totals are given per env and never added together.
- **Times are IST**, written as `2026-11-02 17:30` or "17:30 IST".

## 3. The shell

| Part | Desktop ≥ 1024 px | Tablet 768–1023 px | Phone < 768 px |
|---|---|---|---|
| Left rail (10 sections; the current one expanded) | fixed, 236 px | slides in from ☰ | slides in from ☰ |
| Top bar (crumbs, search, env badge, run state, kill switch, alerts, theme, profile) | sticky | sticky | sticky, compact (icons only) |
| Bottom tab bar (Home, Trade, Sessions, Reports, More) | hidden | hidden | fixed |
| Inside the phone apps (`ATIPApp/` user agent or `?app=1`) | the rail, tab bar, env and run pills hide; the native tabs and banner show them | | |

- **Search (Ctrl/⌘K or /)** finds pages, commands (halt, theme, set up a session, move money, tax, reports, S1–S10) and symbols. Symbols come from `/api/v2/search` and open the stock panel on `/?stock=`.
- **Kill switch.** The button opens a red dialog: "Stop all trading now?". The dialog says that **every** order stops, exits included, and shows the ₹ value of the open trades left without ATIP's stops. It also says how many running sessions will pause. A reason is optional. Resuming stays on Command Center (typed `RESUME`). On the viewer the button reads "View only" and is disabled.
- **Go-to keys:** `g h` / `g s` / `g t` / `g a` / `g r` / `g c` / `g m` / `g l` / `g w` / `g e`. `?` lists them.

## 4. Components (`ux_kit.py` + `atip.css`)

| Component | Notes |
|---|---|
| `tile(label, value, sub, key)` | KPI tile; `key=True` outlines the two that matter (gross, actual) |
| `.ax-answer` | the Home page's three answers; a left rule shows the tone, with a symbol and a word |
| `status(text, tone)` / `mode_status` | pills: good, warn, bad and info. Always a word, never only a colour |
| `table(id, columns, rows, totals, drawer)` | sticky header, sort (click or Enter), filter, column chooser, CSV export (raw values), row → detail drawer, total rows that stay last when sorted, empty state |
| `.ax-drawer` | right-hand panel, Esc closes, focus returns to the opener. Used for Session setup, schedule edit and row details |
| `dialog.ax-dialog` | modal for confirmations; danger variant for the kill switch |
| `.ax-field` + `.ax-err` | label above, help below, error in plain words beside the field (`aria-invalid`) |
| `.ax-banner` | info (market closed), warn (stale data, waiting below minimum), bad (books do not balance), good |
| `.ax-empty`, `.ax-skel` | empty and loading states |
| `.ax-tabs`, `.ax-mar` | page tabs; the Manage / Accounting / Reports switch every module has |
| `progress(p)` | an open cycle between its stop and its target (`role=meter`, labelled) |

## 5. Charts (from the dataviz method)

- **One y-axis**, a recessive grid, 2 px lines and 3 px-radius bars.
- **Series:** series-1 (blue) = actual / totals; series-2 (orange) = gross / deductions / floor.
- **A legend for two or more series, plus a direct label at each line's end.** The figures are always in a table next to the chart.
- **Hover:** a crosshair and tooltip on lines (`data-ax-line`), and a tooltip on each bar (`data-ax-bars`). Each chart has `role=img` with a title.

| Chart | Where | Form |
|---|---|---|
| Equity: gross vs actual | session detail | two lines |
| From gross to actual | session detail, monthly report | waterfall: totals from zero, each charge and the tax taken off |
| Compounding | session detail | line of each cycle's start amount, with the floor dashed |
| All sessions | Sessions page, weekly report | small multiples, each on its own scale |
| Sparkline | session cards, Home | actual equity, last 30 cycles |

## 6. Page states

Every new page handles these:

| State | How it shows |
|---|---|
| Empty | an `.ax-empty` that says what to do |
| Loading | skeleton bars where data loads after the page |
| Error | a red banner with the reason in words; the API answers `{"error", "errors"}` |
| Stale data | a warning banner when scores are more than 3 days old |
| Market closed | an info banner |
| Stopped | the run-state pill, plus a 423 message on any write: "ATIP is stopped, so nothing can change until it resumes" |
| Viewer | 403 on writes, and the kill switch reads "View only" |
