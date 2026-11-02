# ATIP UI/UX redesign: developer handoff (2 Nov 2026)

What was built, where it lives, and how each screen behaves. Read it with the design pack (`ATIP_UIUX_Design_Pack_2026-11-02.md`) and the design system (`ATIP_Design_System_2026-11-02.md`). Screenshots of every top-level page, in both themes at 1440 px and 390 px, are in `docs/testing/screenshots/` (Test server) and `docs/testing/screenshots/prod/` (live-DB snapshot).

## 1. Information architecture

There are ten sections, defined in `dashboard/shell.py` `NAV`. Every older URL keeps its content and is served inside the shell, and nothing redirects away from a page.

| # | Section | Landing page | Pages folded in |
|---|---|---|---|
| 1 | Home | `/home` (new) | — |
| 2 | Markets | `/markets` (new hub) | `/market`, `/market-pulse`, `/global-asia`, `/?tab=news`, `/m?tab=live`, `/data-platform` |
| 3 | Research | `/` (ATIP Scores) | `/research`, `/screener`, `/signals`, `/?tab=hist`, `/options-builder`; stock view through search (`/?stock=SYM`) |
| 4 | Trade | `/command` | `/command/signals`, `/setup`, `/monitor`, `/journal`, `/pnl`, `/trading`, `/baskets`, `/execution-lab`; `/trade` hub |
| 5 | Sessions | `/sessions` (new) | `/sessions/{S1..S10}` (new) |
| 6 | Accounting | `/accounting` (new) | tabs: ledger, session, charges, tax, funds, recon, transfers, trial |
| 7 | Reports | `/reports` (new) | tabs: generate, schedules, archive, custom |
| 8 | Strategy Lab | `/lab` (new hub: run → compare → promote) | `/strategies`, `/strategy-builder`, `/backtests`, `/quant`, `/ml` |
| 9 | Wealth | `/wealth` | its 7 tabs |
| 10 | Settings & Admin | `/settings` (new) | `/account`, `/brokers`, `/admin`, `/admin/console`, `/compliance`, `/privacy`, `/ops` (new page; it was a 404), `/assistant`, `/app`, `/m` |

**`/` stays the ATIP Scores page.** It is the most bookmarked URL and the source of the old phone app's Today tab. The logo, the rail's Home link and the phone's Home tab go to `/home`.

**Shell mechanics** (`dashboard/shell.py`):

- `ShellMiddleware` (pure ASGI) buffers an older page's HTML response and inserts the stylesheet, the frame and the script. It only acts on a GET that returns 200 with `text/html` and no content-encoding.
- It never writes a token into a page. Older pages carry their own `TOKEN` / `ATIP_TOKEN`. New pages get `page_token()` inside their own request, so in enterprise mode each tenant keeps its own token.
- These pass through untouched: `?shell=0`, `/login`, `/api/*`, `/static/*` and `/health*`.

## 2. Screens

Every screen states its primary job in one line under its title, and every module has a **Manage / Accounting / Reports** switch.

### Home (`/home`)
**Job:** is ATIP safe, running and making money today?

The page opens with three answers:

| Question | What it checks or shows |
|---|---|
| **Safe?** | kill switch, feed freshness, Dhan token, LIVE |
| **Running?** | operating state, sessions running, auto-send, last data update |
| **Making money today?** | today's actual P&L, with gross, charges and tax est. beside it, the cycles closed and the open P&L |

Below the answers is a customisable grid: Market, Trade of the Day, Sessions (gross vs actual with sparklines), Needs you, Alerts and Pipeline. The viewer can hide widgets, and the choice is remembered in this browser only. The data comes from `GET /api/v2/home`.

### Sessions (`/sessions`)
**Job:** set each session's money and rules, and see what it really earned.

- **Totals tiles:** starting amounts, equity, gross, charges, tax est., actual, cycles, sessions waiting.
- **A card for each of S1–S10:** mode and status pills, gross vs actual, equity, cycles done / max, floor, sparkline, the open trade with its progress bar, and "Waiting: below minimum (₹x of ₹y)".
- **Small multiples** of every session's actual equity.
- **Worked example (§4.6)** in a help panel.

**Session setup drawer.** Field groups: Identity; Mode and status (AUTO needs the "I understand" tick); Money; Exits; Cycles.

- Money covers the starting amount, the minimum per cycle, the floor rule (FIXED / RATCHET_PCT / STEP with value and every-N) and the compounding basis.
- Every field validates in plain English, for example "Minimum ₹30,000 is more than the ₹25,000 starting amount."
- While a trade is open, the starting amount is locked and the drawer says why.
- Changing the starting amount needs a "restart the books from now" tick, which shows the ₹ before and after.
- The drawer posts to `POST /api/v2/sessions/{sid}/setup`. The server applies the change through `start_pool`, `update_session`, `set_mode` and `set_status`, and each change lands in `session_events`.

**"Split a total…"** calls `POST /api/v2/sessions/split`. It only fills in a proposed amount on each card, and nothing is saved until each session is saved.

### Session detail (`/sessions/{sid}`)
**Manage tab:**

- Every tile from §4.3: starting amount, equity, gross, charges, tax est., actual, return gross / actual, CAGR gross / actual (after 30 days), cycles, win rate, average R, max drawdown, best and worst cycle, current floor, tax set aside.
- The open cycle with its progress bar.
- Charts: the equity line (gross vs actual), the gross → actual waterfall, the compounding line with its floor, and the activity timeline.
- **Every cycle**: the full 4.3 table with itemised charges, sortable and exportable. Clicking a row opens the cycle's full breakdown in a drawer.

**Accounting tab:** the session's Cash account with its running balance, and charges by type.

**Reports tab:** the cycle book as PDF, XLSX, CSV or HTML, with the files made before.

### Accounting (`/accounting?tab=`)
- **ledger**: the general ledger with filters (session, kind, env, FY) and a "books balance" check.
- **session**: one session's cash account.
- **charges**: the charges register by type, session or month.
- **tax**: heads, set-offs, LTCG exemption, liability, provisional vs liability, and the FY's cycles.
- **funds**: VIRTUAL and ACTUAL, side by side and never added together.
- **recon**: contract-note CSV upload, "check cash now", and accept / investigate on each difference.
- **transfers**: move money with a ₹ impact preview and a confirmation. Refused out of a session with an open trade. Also manual adjustments (books only).
- **trial**: the trial balance.

### Reports (`/reports?tab=`)
- **generate**: one card per report, each with period inputs and formats; it shows a skeleton while working, then download links.
- **schedules**: the table, an edit drawer (how often, time in IST, weekday / day / cron, formats, options, on, deliver), delete, and "run due now".
- **archive**: every file made, to download again.
- **custom**: sections, sessions, dates, formats.

### Settings (`/settings`)
- **Credentials:** env badge, Dhan token state, links to Brokers and Account.
- **Risk limits:** read-only, with where to change them, and the kill-switch state.
- **Tax rates:** a form, plus the "estimate, not tax advice" banner.
- **Notifications, display and administration.**

### Hubs and others
- `/markets` and `/trade`: hub pages.
- `/lab`: run → compare → promote.
- `/more`: every section; the phone's More tab.
- `/ops`: `/health`, `/health/ready`, `/api/status` and the latest job runs.

## 3. APIs (`dashboard/ux_routes.py`)

**Reads (GET):**

| Area | Routes |
|---|---|
| Shell | `/api/v2/shell/state` |
| Search and Home | `/api/v2/search?q=`, `/api/v2/home` |
| Sessions | `/api/v2/sessions`, `/api/v2/sessions/{sid}` |
| Accounting | `/api/v2/accounting/{ledger, trial-balance, session/{sid}, charges, tax, funds, recon}` |
| Tax | `/api/v2/tax/config` |
| Reports | `/api/v2/reports/{kinds, schedules, archive, archive/{id}/download}` |

`/api/status` gains an `env` key. Every existing route and key is unchanged.

**Writes (POST, `X-ATIP-Token`):**

| Area | Routes |
|---|---|
| Sessions | `/api/v2/sessions/{sid}/setup`, `/api/v2/sessions/split`, `/api/v2/sessions/sync` |
| Accounting | `/api/v2/accounting/{transfer, adjust, recon/import, recon/cash, recon/{id}}` |
| Tax | `/api/v2/tax/config` |
| Reports | `/api/v2/reports/{generate, schedules, schedules/{id}, schedules/{id}/delete, run-due}` |

Every write is refused with 423 while ATIP is stopped and with 403 on the viewer, through the existing middlewares. Validation errors return `400 {"error", "errors": {field: message}}`.

## 4. Data (additive only)

- `trading_sessions` + `floor_rule`, `floor_value`, `floor_step_every`, `compound_basis` (`orders/sessions.py` POOL_COLUMNS). NULL means FIXED / CASH_AFTER_CHARGES, which is the old behaviour.
- `session_cycles`: one row per closed cycle, holding the full 4.3 figures, FY, env and exit reason (`accounting/cycles.py`).
- `session_transfers` (`accounting/cycles.py`).
- `ledger_entries` (`accounting/ledger.py`).
- `recon_items` (`accounting/reconcile.py`).
- `report_schedules` and `report_archive` (`enterprise/session_reports.py`).

All of these are created on first use with `CREATE TABLE IF NOT EXISTS` / `ALTER TABLE ADD COLUMN`.

## 5. Engine changes

These are the only places where trading behaviour can differ, and only when the owner opts in:

- `orders/sessions.py ledger()` adds keys: `transfers`, `floor`, `floor_rule`, `compound_basis`, `tax_reserve`, `investable_cash` and `waiting`. Cash includes transfers, which is 0 with none.
- `gate()` uses `floor` and `investable_cash`. With the defaults they equal `min_buy_value` and `cash`, so behaviour is unchanged.
- `pool_quantity()` sizes from `investable_cash`, which equals `cash` by default.

Under AFTER_TAX_RESERVE the next buy is smaller by the tax set aside. Under RATCHET_PCT or STEP the entry can wait longer. Both are more conservative, never less.

Exit legs still pass `gate()` untouched. The kill switch, PAPER-only auto-send, AUTO's ack and the SEBI Algo ID guard are not changed.

## 6. Accessibility

Built into the pages:

- `lang="en-IN"`, a skip link, and landmarks (`header`, `nav` with labels, `main`).
- One `h1` per page.
- Every field is labelled and every button named; the ten "Setup" buttons are named "Set up S1"…
- Visible focus rings. Esc closes drawers and returns focus to the opener.
- Sortable headers work by keyboard, and table rows open by Enter.
- `role=status` / `aria-live` on the live state, and `role=meter` on progress bars.
- Signs as well as colours; contrast ≥ 4.5:1 in both themes; `prefers-reduced-motion` respected.

Checked by `tests/test_ux_shell.py`, using a structural audit and a token contrast check.

## 7. Acceptance checks (automated)

| Test file | What it checks |
|---|---|
| `tests/test_session_cycles.py` | 4.6 to the paisa, whole shares, losses, floors, both bases, FY set-off, ledger balance, reconciliation, transfers |
| `tests/test_session_reports.py` | every kind in every format, env and disclaimer, schedules and cron, viewer |
| `tests/test_ux_shell.py` | every old URL in the shell, new pages, app mode, setup validation, token, 423, 403, reports, tax config, search, a11y |
| `tests/test_w39_ui_smoke.py` (Playwright, where installed) | the new pages in both themes at 1440 and 390, the palette and the setup drawer |
