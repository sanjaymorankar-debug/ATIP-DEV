# ATIP UI/UX redesign: design-thinking pack

**Date:** 2 Nov 2026 · **For:** the full UI/UX redesign brief (`ATIP_UI_UX_Redesign_Prompt_2026-11-02.md`) · **Status:** design input for the 2 Nov build

This pack covers the five design-thinking stages: Empathise, Define, Ideate, Prototype and Test. The findings come from reading the current code: `dashboard/server.py` (`/`), `dashboard/command_page.py` and `command_more.py` (`/command*`), `dashboard/w38_page.py` (`/compliance`, `/m`), `dashboard/w39_page.py` (research pages), `dashboard/enterprise_page.py` and `saas_page.py` (`/account`, `/app`), `orders/sessions.py`, `pipeline/operating_schedule.py`, `ops/role.py`, `enterprise/reports.py`, and the earlier redesign in `docs/UX_REDESIGN_2026-10.md` (UXR-01…10).

Words used throughout:
- A **cycle** is one buy with all of a session's cash plus the sale that closes it. The code calls it an "occurrence".
- **Gross P&L** = sell value − buy value.
- **Net before tax** = gross − charges. This is today's `pnl`.
- **Actual P&L** = net before tax − estimated tax.
- Money is shown as ₹ with Indian grouping (₹1,25,000.00). Times are IST.

---

## 1. Empathise

### 1.1 Persona A: the owner-trader

| | |
|---|---|
| Who | ATIP's owner and only trader. Runs ATIP on his Mac alongside a day job. PAPER today, LIVE later. |
| Money set-up | 10 independent paper sessions S1–S10 of ₹25,000 each (₹2,50,000 in all). Exit rules: −2 % stop; from +3 % the stop trails 1 % under the high; after 15 days a price at +3 % or more sells. Each cycle reinvests the whole cash. |
| Devices | The Mac at 1440 px at his desk. At breaks, his phone: the Android/iOS WebView apps or `/m`. |
| Goals | Trust the automation without watching every tick. Know what each session really earns. Let winners grow from their profits. Have a tax figure ready for his CA at FY end. |
| Fears | An order going out on stale data or after a bad morning. A figure that looks like profit but is before charges and tax. Changing the wrong session's money. |
| Uses today | `/command`, `/command/pnl`, `/command/journal`, `/` (scores), Telegram, and the CLI `python -m orders.sessions`. |

**Jobs to be done**

| Moment | When… | I want to… | So that I can… |
|---|---|---|---|
| Pre-market, 08:30 | …the brief is ready | …see the overnight and Asia picture, today's top signals, and whether all 10 sessions are armed with cash above their minimum | …decide in ten minutes what to allow today |
| Intraday, 09:15–15:30 | …a trigger fires, or I have a minute between meetings | …confirm or reject it with the live-vs-signal gap and the ₹ at risk in view, and see each session's cycle progress | …act in one tap and get back to work |
| Any time | …something looks wrong | …stop everything from the page I am on | …not have to hunt for the switch |
| Post-market, ~16:45 | …the pipeline has run | …see what each session closed today: gross, charges, estimated tax and actual | …know whether a strategy works after costs |
| Weekend | …the market is shut | …compare sessions, move money from a weak one to a strong one, change minimums and exit rules, and promote a tested strategy | …plan next week |
| Month end | …the month closes | …download that month's P&L statement as a PDF | …keep records without assembling them |
| FY end (Mar–Apr) | …the FY closes | …see realised gains by tax head, with set-offs and the LTCG exemption applied | …hand my CA a figure and file early |

### 1.2 Persona B: the read-only viewer

| | |
|---|---|
| Who | Anyone using the web copy (`ATIP_ROLE=viewer`, `ops/role.py`). Most often the owner away from his Mac, on an office PC or a phone browser. Sometimes a trusted reader, such as a family member or the CA at FY end. |
| What the copy is | A copy of the Mac's database, pushed every 10 minutes by `ops/viewer_sync.py`. It holds no keys and runs no scheduler. Every POST/PUT/PATCH/DELETE is refused with 403 (`install_guard`). |
| Goals | Check status and money with no risk of changing anything, and know how fresh the data is. |
| Pain today | Write buttons look live but fail. For example, "Halt all trading" on `/command` returns a 403 in a browser `alert()`. The role banner gives the copy's time, but no figure shows its own age. |

**Jobs to be done.**
- When I open the link away from the Mac, I want the same five-second answer as on the Mac plus "data from the Mac at HH:MM IST", so that I know how far to trust it.
- When I see a problem, I want to be told plainly that it can't be fixed here and where it can, so that I never think I halted something when I didn't.
- When the CA asks, I want to open the FY tax statement or download it from the archive, so that nobody needs the Mac.

### 1.3 Day-in-the-life journey map (owner)

The run state comes from `/api/status`, built in `pipeline/operating_schedule.py`. The post-market pipeline runs at `POSTMARKET_RUN_TIME = "16:45"` in `pipeline/scheduler.py`.

| Stage (IST) | ATIP state | Goal | Looks at today | Pain today | Opportunity |
|---|---|---|---|---|---|
| 21:00–06:00 night | STOPPED, "ATIP stopped — resumes 06:00 IST" | Nothing should trade. Maybe review the day | `/m` on the phone, Telegram | Write buttons stay enabled and fail with 423 only after a click: mode buttons on `/command`, "Start the sessions" on `/command/pnl`. Only the kill switch, the schedule override and sign-in are allowed (`READ_ONLY_ALLOWED`), and nothing on screen says so | The shell disables write controls with "Available from 06:00 IST". Halt stays live and says it works while stopped |
| 06:00 resume | RESUMING, "ATIP resuming — running the 06:00 sequence" | Know the night's catch-up worked | The banner on any page ("step 3/6 …") | A failed step shows only in `pipeline_log` or the `/health` JSON. `/compliance` links to `/ops`, which has no route | A Home pipeline card lists the six steps with ✓ or ✗, plus "Re-run missed jobs" (`POST /api/health/recover`) |
| 07:00–08:30 pre-market and brief | RUNNING | Read the brief, see today's top signals, check the sessions are armed | The 08:30 Telegram digest, `/`, `/market-pulse`, `/global-asia`, `/command` | Four pages, each with its own navigation. The `/` KPI row is market-only (Market Health … USD/INR). A session waiting because its cash is below `min_buy_value` shows only as a gate reason on `/command/monitor` | Home has the five-second status, a Market card and "Sessions at a glance", which shows "Waiting: below minimum (₹x of ₹y)" |
| 09:15–15:30 market | RUNNING, live feeds on | Confirm or reject fast, watch cycles, stop if needed | `/command`, `/command/monitor`, Telegram | The kill switch exists only on `/command*`. From `/research`, `/trading` or `/market` it is 2–3 hops away. The pending strip on `/` shows symbol, side, quantity and price, but not the session or the stop | The kill switch and the pending count sit in the shell on every page. Live Monitor shows each session's cycle progress |
| ~16:45 post-market | RUNNING, post-market pipeline | What did each session make today, after costs? | `/command/pnl`, `/command/journal` | P&L is net of charges only, and tax is absent. Three different numbers share one name: "P&L" on Journal, "P&L" (change in equity) on P&L, and "Booked" | The Daily session summary report runs after the pipeline. Sessions shows gross, charges, tax (est.) and actual per cycle |
| 17:00–21:00 evening | RUNNING (evening jobs 19:00–20:50) | Plan tomorrow, adjust a session | `/command/setup`, `/command/pnl`, the CLI | A per-session amount can be set only by CLI or API (`python -m orders.sessions --pools 40000 --ids S3`, `POST /api/sessions/pools {"sid": …}`). The UI form gives every session the same amount. Errors show field names ("min_buy_value Rs 30,000 is more than the Rs 25,000 capital") | A Session setup drawer for each session, with plain-English checks and a confirmation that shows the ₹ impact |
| Weekend | RUNNING. Resume steps 1–5 still run, live feeds stay off. The weekly job runs Sat 08:00 | Compare sessions, move money, promote a strategy | `/command/journal`, `/backtests`, `/strategies` | Sessions cannot pass money to each other (the `/command/pnl` note says "Sessions do not lend to each other"). No path takes a backtest to a session. There is no weekly report | Sessions comparison as small multiples, Move money, Strategy Lab run → compare → promote, and a Weekly performance report on Monday 08:00 |
| Month end, FY end | — | A statement, and the tax figure | Reports on `/app` (no page links to it), browser print | There is no PDF engine and no P&L or tax statement kind (`enterprise/reports.py`). `wealth/rebalance.py` says tax is out of scope | Reports: the Monthly P&L statement and FY tax statement in PDF, XLSX, CSV and HTML, plus an archive |

**Viewer journey (short).**

| Stage | Goal | Pain today | Opportunity |
|---|---|---|---|
| Opens the link | Is the data fresh? | The role banner names the copy's time, but figures carry no age. Data can be up to 10 min old | The shell shows "VIEWER · data from the Mac at 10:40 IST (2 min ago)" next to the environment badge, and it turns amber after 20 min |
| Sees trouble | Stop it | "Halt all trading" is visible and clickable on `/command`, then fails with 403 | The shell shows a disabled "Halt runs on the Mac" with the reason. Alerts still show |
| FY end, for the CA | Get the tax statement | No tax statement exists. `/app` reports need sign-in and no page links to them | Reports › Archive is readable. Accounting › Tax register shows only if the owner allows it (see §6) |

---

## 2. Define

**Point of view.** The owner needs one trustworthy picture of safety, run state and money, and a way to manage each session's money himself. Today:
- Those facts are spread over `/`, `/command`, `/command/pnl`, `/command/journal`, `/health` and the CLI.
- Each of those 31 pages looks different.
- Every money figure stops at "net of charges", so nothing shows what he really earned.

**How might we…**
1. …let the owner know **in five seconds whether ATIP is safe, running and making money**, from any page?
2. …show **what each session really earned after charges and tax**, never blending gross and actual into one number?
3. …let him **set each session's money without touching the CLI**: starting amount, minimum per cycle and how the minimum grows?
4. …put "stop everything" one click away on every page, and keep it working at night when everything else is read-only?
5. …make the environment (PAPER, SANDBOX, LIVE) and the run state impossible to miss on both copies, using a word and not only a colour?
6. …make every rupee that moves between sessions deliberate, explained and logged: a reason, the ₹ impact before confirming, and an entry in `session_events`?
7. …show what a minimum rule will do before it is saved, for example "next cycle needs ₹13,205; S3 has ₹26,410"?
8. …turn the FY-end tax question into one CA-ready screen that is clearly labelled as an estimate?
9. …get the right report to him on schedule, as a PDF, without his having to remember to ask?
10. …let him reach any of the 31 pages, any stock or any command in two steps?
11. …make the viewer copy obviously read-only, and say how fresh it is, without buttons that do nothing?
12. …give every page one look, one table pattern and one number format, so that a figure means the same thing everywhere?

**Success measures**

| Measure | Target |
|---|---|
| Five-second test on Home (owner names safe, running and today's actual P&L) | 3 of 3 correct within 5 s |
| Top-10 tasks within 3 clicks | at least 7 of 10 |
| Session money changed with no CLI | 100 % (amount, minimum rule, transfers) |
| Old URLs still working | 31 of 31 |
| WCAG 2.1 AA (contrast, focus, keyboard) | no failures on new pages |

---

## 3. Ideate

### 3.1 Three navigation options

**A. Left-rail workspace (10 sections).** The shell has two parts. A left rail lists the 10 sections, with the active section's pages beneath it. A top bar holds search, the environment badge, the run-state banner, alerts, the kill switch and the profile. Every module has the same sub-tabs: Manage, Accounting, Reports. Ctrl/Cmd-K opens a palette for stocks, pages and commands. On a phone, the rail becomes a bottom bar with five tabs.

```text
┌ rail ───────┬ [Search  Ctrl K]  [PAPER]  ● ATIP running  Alerts 2  [■ Halt all trading]  SM
│ Home        │ Section › Page                       [Primary]   [Manage]  Accounting  Reports
│ Markets     │
│ Research    │   existing page body, restyled with the shared tokens
│ … 10 items  │
└─────────────┴──────────────────────────────────────────────────────────────────────────────
phone: top bar (PAPER · ● Running · Halt)  +  bottom bar Home · Trade · Sessions · Reports · More
```

**B. Hub-and-spoke command center.** The Command Center becomes the hub. It shows status, P&L, the waiting list and the 10 session tiles, plus a tile for each area. Each spoke is a full page with "◀ Hub", the badge and Halt in its header. There is no global navigation, and search lives on the hub only.

```text
                 ┌─────────── Hub ───────────┐
                 │ status · P&L · waiting    │
                 │ S1 … S10 tiles            │
                 │ [Markets] [Research] …    │
                 └───────────────────────────┘
spoke page:  [◀ Hub]   Page title ……………………   [PAPER]  [■ Halt]
```

**C. Task-based top nav.** Six verb menus, each opening a mega-menu: Monitor, Find, Trade, Money, Review, Configure. The search box sits in the top bar.

```text
ATIP  Monitor▾  Find▾  Trade▾  Money▾  Review▾  Configure▾      [Search]  [PAPER]  [■ Halt]
      Money ▸ Session P&L · Session setup · Transfers · Ledger · Tax · Funds
phone: ☰ → six verb groups → page
```

### 3.2 Clicks to the top 10 tasks

How the clicks were counted:
- The owner starts mid-work, on Research › Screener (`/screener`).
- Each click, tap, key chord or Enter counts as 1. Typing a value counts 0.
- A required confirmation dialog counts.
- "—" means the task cannot be done in the UI today.

| # | Task | Today | A | B | C |
|---|---|---|---|---|---|
| T1 | Is ATIP safe and making money today? | 3, partial (← Dashboard, Command Center, P&L; no tax) | 1 | 1 | 2 |
| T2 | Give S3 ₹40,000 and a ratcheting minimum | — (CLI; no minimum rules) | 5 | 6 | 6 |
| T3 | What did S1 really earn after charges and tax? | 4, partial (net only) | 1 | 2 | 2 |
| T4 | Stop everything now | 4 | 2 | 2 | 2 |
| T5 | Download last month's P&L statement as PDF | — (6 clicks to browser-print `/command/pnl`, which is not a statement) | 2 | 3 | 3 |
| T6 | What tax do I owe for FY 2026-27? | — | 2 | 3 | 2 |
| T7 | Move ₹5,000 from S2 to S4 | — (sessions do not lend) | 6 | 6 | 6 |
| T8 | Check a pending confirmation | 2 (← Dashboard, Confirm) | 2 | 2 | 3 |
| T9 | Find a stock's ATIP score | 2 (← Dashboard, type in the filter, row) | 2 | 3 | 2 |
| T10 | Schedule the weekly report | — (`/app` is not linked; no weekly session report) | 3 | 4 | 4 |
| | **Total** | 5 of 10 possible | **26** | **32** | **32** |

How A gets its counts:
- **T1:** one click to Home. Safe and running are already in the top bar at 0 clicks.
- **T2:** Sessions → S3 Setup → minimum rule → Save → Confirm.
- **T3:** the S1 card on Sessions shows "A ▲ +₹542".
- **T5:** the archive row's PDF link on the Reports landing.
- **T7:** Sessions → S2 ⋯ → Move money → To S4 → Move → Confirm.
- **T8:** Alerts → Confirm & send.
- **T9:** Ctrl K → Enter.
- **T10:** Reports → Weekly performance › Schedule → Save. The default is Monday 08:00 IST.

### 3.3 Scores (1 = poor, 5 = best)

Clicks score 5 at 28 or fewer, 4 at 29–33 and 3 at 34–38.

| Criterion | A rail | B hub | C task nav |
|---|---|---|---|
| Findability: can the owner say where a page lives without searching? | 5. All ten sections are always visible with nouns he already uses; the active section lists its pages; search covers the rest | 3. 31 pages sit behind tiles on one page; every spoke-to-spoke move goes through the hub | 3. Verbs are ambiguous: is Tax under Money or Review? Are Signals under Find or Trade? Mega-menus hide items until opened |
| Clicks to the top 10 tasks | 5 (26) | 4 (32) | 4 (32) |
| Mobile fit at 390 px | 4. Five tabs fit a bottom bar; the other five sections cost one extra tap under More | 4. A hub grid suits a phone, but going back between spokes costs taps | 2. Six mega-menus become nested hamburger menus, and the verbs lose their meaning |
| **Total (of 15)** | **14** | **11** | **9** |

Safety visibility was not scored. All three options carry the kill switch, the environment badge and the run state on every page, because the brief requires it.

### 3.4 Decision: Option A

**A wins on findability and clicks, and stays strong on mobile.**
- **It matches how the owner already talks.** He says "Sessions", "Reports" and "tax". He does not say "Money ▸".
- **Status stays in view while working.** The rail and the top bar stay on every page, so going from Research to Sessions to Accounting never passes through a hub (B) or a menu (C), and "safe" and "running" are answered at 0 clicks.
- **It fits the codebase without a framework.** The shell is static server-rendered HTML around each existing page body, so no SPA is needed (brief §8), and every old URL can render inside it unchanged. Option C would mean relabelling all 31 pages by verb. Option B would need a second navigation model for spokes.
- **It scales.** New pages join a section's sub-list without changing the top level.
- **On mobile, the rail becomes a five-tab bottom bar:** Home, Trade, Sessions, Reports and More. These are the five most-used areas by journey stage. More holds Markets, Research, Accounting, Strategy Lab, Wealth and Settings & Admin.
- **Ctrl/Cmd-K reaches any page, symbol or command in two steps:** open, then Enter.

### 3.5 The chosen information architecture

| # | Section | Pages |
|---|---|---|
| 1 | **Home** | `/home`: today, the five-second status, total P&L across sessions, alerts, pipeline |
| 2 | **Markets** | `/market`, `/market-pulse`, `/global-asia`, `/data-platform` |
| 3 | **Research** | `/` (ATIP Scores with its tabs), `/research`, `/screener`, `/signals`, `/options-builder` |
| 4 | **Trade** | `/command`, `/command/signals`, `/command/setup`, `/command/monitor`, `/command/journal`, `/trading`, `/baskets`, `/execution-lab` |
| 5 | **Sessions** | `/sessions`, `/sessions/S1` … `/sessions/S10` (new) |
| 6 | **Accounting** | `/accounting` (new): general ledger, session ledger, charges register, tax register, funds, reconciliation, transfers |
| 7 | **Reports** | `/reports` (new): catalogue, schedules, archive, custom |
| 8 | **Strategy Lab** | `/strategies`, `/strategy-builder`, `/backtests`, `/quant`, `/ml` |
| 9 | **Wealth** | `/wealth` |
| 10 | **Settings & Admin** | `/settings` (new hub), `/account`, `/brokers`, `/admin`, `/compliance`, `/privacy`, `/assistant`, `/health` |

Every old URL keeps working and renders inside the shell. Some pages are not in the table above. They go here:
- `/command/pnl` becomes Trade › Accounting, and Sessions › Accounting links to it.
- `/app` and `/admin/console` go under Settings & Admin.
- `/login` stays outside the shell.
- `/m` stays the read-only PWA, and its Today view links to `/home`.
- `/compliance` currently links to `/ops`, which has no route. That link moves to Settings › Health.
- `/` still opens ATIP Scores with Research highlighted, so bookmarks behave as before. The logo and the mobile Home tab go to `/home`.

**Manage / Accounting / Reports in every module**

| Section | Manage (configure, act) | Accounting (the money it touched) | Reports (its own report, schedulable) |
|---|---|---|---|
| Home | Today; widget layout | All-sessions gross → charges → tax (est.) → actual | Daily session summary; pre-market brief |
| Markets | The four market pages | Empty state: "Markets places no orders, so no money moves here." | Pre-market brief (08:30), post-market wrap |
| Research | Scores and their tabs, screener, signals, options builder | P&L of trades that began from a signal | Hit rate by score band (from Journal), saved screens |
| Trade | Command Center, Signal Explorer, Setup, Monitor, Journal, Trading, Baskets, Execution lab | Fills and charges per order (`/command/pnl`) | Daily trade log, execution quality |
| Sessions | Cards, Session setup drawer, mode and status, Move money | Every cycle with the full §4.3 columns; session ledger | Session cycle book, daily session summary |
| Accounting | Ledgers, registers, funds, reconciliation, transfers, adjustments | (this is the accounting) | Monthly P&L statement, FY tax statement, charges register |
| Reports | Catalogue, schedules, archive, custom | A check of each report's totals against the ledger | (this is the reporting) |
| Strategy Lab | Run → compare → promote | Simulated charges and slippage vs live paper results | Backtest report, strategy comparison |
| Wealth | The 7 tabs: Overview, Wealth, Goals, Allocation, Rebalance, Performance, Advisor | Holdings P&L (existing performance attribution) | Existing performance reports (`/api/wealth/performance/reports`) |
| Settings & Admin | Credentials, risk limits, tax configuration, notification channels, schedule, account, admin | Plan and billing (`/app`) | Compliance and audit export (19:30 job) |

### 3.6 The shell, specified

| Element | Behaviour |
|---|---|
| Left rail | 10 sections. The active section shows its pages indented. It collapses to icons with labels between 721 and 1199 px. `aria-current` marks the open page |
| Global search (Ctrl/Cmd-K) | Results are grouped as Stocks (opens the stock drawer: ATIP score, signal, EOD date, live gap), Pages (all 31 plus the new ones) and Commands (Halt, New trade setup for X, Set up S3, Move money, Run report…). Open and Enter: 2 steps. The field is always visible with its "Ctrl K" hint |
| Environment badge | Always shows the word: **PAPER** in amber ("virtual money"), **SANDBOX** in blue ("Dhan test host"), **LIVE** in red ("real money"). It reads `broker_env`. Colour is never the only signal |
| Run-state banner | The exact `/api/status` `banner` wording: "ATIP running", "ATIP stopped — resumes 06:00 IST" or "ATIP resuming — running the 06:00 sequence", followed by the suffixes the page already adds ("stops 21:00 IST", "step n/6", "read-only…"). Green, brown-orange or blue as `global_asia.py` does today. On the viewer it adds "VIEWER · data from the Mac at HH:MM IST" |
| Kill switch | A red "Halt all trading" button on every page. The dialog states the consequences, as `/command` does. Once halted it becomes "Halted · Resume…", and a red banner shows on every page. Resuming says that sessions stay paused. The button stays enabled while ATIP is STOPPED, because the guard allows it. On the viewer it is replaced by a disabled "Halt runs on the Mac" |
| Notifications | Badge count of pending confirmations, alerts (`/api/alerts`), finished reports and pipeline problems. A pending item can be confirmed or rejected in the panel, with the same slippage re-check |
| Profile | Theme (System, Light, Dark), role (TRADER or VIEWER), sign out |
| Page header | Breadcrumb (Section › Page). The primary action at top right. The Manage, Accounting and Reports tabs |
| Mobile (≤ 720 px) | Top bar: logo, environment badge, run-state dot with its word, a "Halt" button and a search icon. Bottom bar: Home, Trade, Sessions, Reports, More. Touch targets are at least 44 px |
| Standard states | **Empty:** what to do next. **Loading:** skeleton rows. **Error:** plain sentence plus Retry. **Stale:** amber "data 12 min old". **Market closed:** "NSE closed · opens 09:15 IST"; live columns greyed. **Stopped:** write controls disabled with "Available from 06:00 IST" |
| Numbers | ₹ with Indian grouping. Gains are green with ▲ and +, losses red with ▼ and −. Tabular figures. IST on every timestamp. Every money figure is tagged VIRTUAL or ACTUAL |

---

## 4. Prototype

These are low-fidelity wireframes: desktop at 1440 px for all ten sections, and a 390 px phone version of Home and Sessions. The high-fidelity mockups are the built pages themselves. They are captured as screenshots in light and dark themes, at 1440 and 390 px, in `docs/testing/screenshots/`. This pack does not link to individual images.

### 4.1 Home (`/home`)
Primary job: answer "is ATIP safe, running and making money today?" in five seconds.

```text
┌──────────────────┬────────────────────────────────────────────────────────────────────────────────────────┐
│ ▣ ATIP           │ [Search stocks, pages, commands…  Ctrl K]  [PAPER]  Alerts 2  [■ Halt all trading]  SM │
│                  ├────────────────────────────────────────────────────────────────────────────────────────┤
│ ▶ Home           │ ● ATIP running · stops 21:00 IST · last updated 10:42 IST                              │
│   Markets        ├────────────────────────────────────────────────────────────────────────────────────────┤
│   Research       │ Home › Today                               [Customise]   [Manage]  Accounting  Reports │
│   Trade          │ Mon 2 Nov 2026 · 10:42 IST · every figure VIRTUAL (paper) unless marked ACTUAL         │
│   Sessions       │ ┌─ Is ATIP safe, running and making money? ──────────────────────────────────────────┐ │
│   Accounting     │ │ ✓ SAFE      Not halted · broker OK · live feed 6 s · loss used ₹0 of ₹5,000        │ │
│   Reports        │ │ ● RUNNING   8 of 10 sessions running · 3 on AUTO (ask first) · pipeline OK         │ │
│   Strategy Lab   │ │ ▲ MONEY     Today ▲ +₹1,240 gross · ▲ +₹1,032 actual · since start ▲ +₹13,349.52   │ │
│   Wealth         │ └────────────────────────────────────────────────────────────────────────────────────┘ │
│   Settings       │ ┌─ Total P&L · all sessions ─────────────┐ ┌─ Waiting for you · 2 ──────────────────┐  │
│                  │ │ Gross P&L        ▲ +₹18,420.00         │ │ BUY 6 TCS @ ₹3,912.40 · S3 · EOD signal│  │
│                  │ │ Charges          ▼ −₹1,733.10          │ │ live ₹3,918 (▲ +0.1 %) · expires 10:55 │  │
│                  │ │ Tax (est.)       ▼ −₹3,337.38          │ │               [Reject] [Confirm & send]│  │
│                  │ │ Actual P&L       ▲ +₹13,349.52         │ │ SELL 40 ITC @ ₹432.10 · S7 · target    │  │
│                  │ │ on ₹2,50,000     ▲ +5.34 % actual      │ │               [Reject] [Confirm & send]│  │
│                  │ │ ╱╲╱‾‾ gross   ╱╲_╱‾ actual · 30 days   │ │                                        │  │
│                  │ │                                        │ │                                        │  │
│                  │ └────────────────────────────────────────┘ └────────────────────────────────────────┘  │
│                  │ ┌─ Sessions at a glance (actual P&L) ────┐ ┌─ Alerts ───────────────────────────────┐  │
│                  │ │ S1 ▲+542  S2 ▼−202  S3 ▲+1,210  S4 ·0  │ │ ! S9 waiting: below minimum            │  │
│                  │ │ S5 ▲+88   S6 ▲+310  S7 ▼−64     S8 ·0  │ │   (₹11,400 of ₹12,000)                 │  │
│                  │ │ S9 waiting: below minimum   S10 OFF    │ │ ! Live feed 70 s old at 10:20 (cleared)│  │
│                  │ │ ▃▁▇·▂▄▁· 10 small bars        [All →]  │ │                                        │  │
│                  │ └────────────────────────────────────────┘ └────────────────────────────────────────┘  │
│                  │ ┌─ Market ───────────────────────────────┐ ┌─ Pipeline ─────────────────────────────┐  │
│                  │ │ Health 62 · Nifty ▲ +0.4 % · VIX 13.2  │ │ 06:00 resume ✓ 6/6 · 08:30 brief ✓     │  │
│                  │ │ Trade of the Day: …          [Set up →]│ │ 16:45 post-market: scheduled           │  │
│ ‹ Collapse       │ └────────────────────────────────────────┘ └────────────────────────────────────────┘  │
└──────────────────┴────────────────────────────────────────────────────────────────────────────────────────┘
```

The status block always uses the words SAFE, RUNNING and MONEY together with a symbol. When a check fails, the word changes: "HALTED", "STOPPED", "LOSING TODAY". On a day with no closed cycle, the money line reads "No cycle closed today", not "₹0".

### 4.2 Markets (`/market-pulse`; also `/market`, `/global-asia`, `/data-platform`)
Primary job: show where the market stands before and during the session.

```text
┌──────────────────┬────────────────────────────────────────────────────────────────────────────────────────┐
│ ▣ ATIP           │ [Search stocks, pages, commands…  Ctrl K]  [PAPER]  Alerts 2  [■ Halt all trading]  SM │
│                  ├────────────────────────────────────────────────────────────────────────────────────────┤
│   Home           │ ● ATIP running · stops 21:00 IST · last updated 10:42 IST                              │
│ ▶ Markets        ├────────────────────────────────────────────────────────────────────────────────────────┤
│    Overview      │ Markets › Market pulse                       [Refresh]   [Manage]  Accounting  Reports │
│    Pulse         │ Pre-open · NSE opens 09:15 IST · live feed 6 s · EOD scores 31 Oct 2026                │
│    Global & Asia │ ┌─ Nifty 50 ───────────────┐ ┌─ India VIX ──────────────┐ ┌─ GIFT Nifty ─────────────┐ │
│    Data platform │ │ 24,612.30  ▲ +0.42 %     │ │ 13.20  ▼ −2.1 %          │ │ 24,655  ▲ +0.18 %        │ │
│   Research       │ │ ▁▂▃▅▆▅▇ intraday         │ │ calm (under 15)          │ │ at 08:42 IST             │ │
│   Trade          │ └──────────────────────────┘ └──────────────────────────┘ └──────────────────────────┘ │
│   Sessions       │ ┌─ Breadth & sectors ────────────────────┐ ┌─ FII / DII (provisional) ──────────────┐  │
│   Accounting     │ │ Advances 1,412 · declines 688          │ │ FII ▼ −₹1,204 cr · DII ▲ +₹1,880 cr    │  │
│   Reports        │ │ IT ▲ +1.2  Bank ▲ +0.3  Metal ▼ −0.8 … │ │ 5-day bars ▂▅▃▇▄                       │  │
│   Strategy Lab   │ │ [heat-map of 12 sectors, sign + colour]│ │ News & impact: 3 high-impact →         │  │
│   Wealth         │ │                                        │ │                                        │  │
│   Settings       │ └────────────────────────────────────────┘ └────────────────────────────────────────┘  │
│                  │ Accounting tab: empty state — "Markets places no orders, so no money moves here."      │
│ ‹ Collapse       │ Reports tab: pre-market brief (08:30) and post-market wrap, each [HTML] [PDF].         │
└──────────────────┴────────────────────────────────────────────────────────────────────────────────────────┘
```

### 4.3 Research (`/`, ATIP Scores)
Primary job: find and judge a stock by its ATIP score.

```text
┌──────────────────┬────────────────────────────────────────────────────────────────────────────────────────┐
│ ▣ ATIP           │ [Search stocks, pages, commands…  Ctrl K]  [PAPER]  Alerts 2  [■ Halt all trading]  SM │
│                  ├────────────────────────────────────────────────────────────────────────────────────────┤
│   Home           │ ● ATIP running · stops 21:00 IST · last updated 10:42 IST                              │
│   Markets        ├────────────────────────────────────────────────────────────────────────────────────────┤
│ ▶ Research       │ Research › ATIP Scores                    [Export CSV]   [Manage]  Accounting  Reports │
│    ATIP Scores   │ Scores · Top VPI · Buy zones · RRI · MRI · CRI · History   │  News, FII/DII: Markets › │
│    Research      │ [Filter symbol…] [Signal ▾] [Sector ▾] [Score ≥ __]  [Columns ▾]   EOD 31 Oct 2026     │
│    Screener      │ ┌──────────────────────────────────────────────────┐ ┌─ TCS · stock drawer ───────────┐│
│    Signals       │ │ Stock     Signal  ATIP  VPI  MRI  CRI  Close ₹ ▾ │ │ ATIP 81 · BUY · EOD 31 Oct     ││
│    Options       │ │ ──────────────────────────────────────────────── │ │ Live ₹3,918 (gap ▲ +0.4 %)     ││
│   Trade          │ │ TCS       BUY      81   74   69   12   3,904.10  │ │ Why: VPI ▇▇▇▇  MRI ▇▇▇         ││
│   Sessions       │ │ ITC       BUY      78   70   66   18     430.55  │ │ CRI 12 (lower is better)       ││
│   Accounting     │ │ HDFCBANK  HOLD     64   58   61   22   1,712.00  │ │ [Set up a BUY →]               ││
│   Reports        │ │ … sticky header · sort · filter · 50 a page      │ │ [Open full stock view]         ││
│   Strategy Lab   │ │                                                  │ │                                ││
│   Wealth         │ │                                                  │ │                                ││
│   Settings       │ └──────────────────────────────────────────────────┘ └────────────────────────────────┘│
│                  │                                                                                        │
│ ‹ Collapse       │                                                                                        │
└──────────────────┴────────────────────────────────────────────────────────────────────────────────────────┘
```

The tabs on `/` become real `role="tab"` buttons. News, FII/DII and Global & Asia stay on `/` for bookmarks, and each carries an "Also in Markets ›" pointer.

### 4.4 Trade (`/command`)
Primary job: act on what needs the owner now, inside the risk budget.

```text
┌──────────────────┬────────────────────────────────────────────────────────────────────────────────────────┐
│ ▣ ATIP           │ [Search stocks, pages, commands…  Ctrl K]  [PAPER]  Alerts 2  [■ Halt all trading]  SM │
│                  ├────────────────────────────────────────────────────────────────────────────────────────┤
│   Home           │ ● ATIP running · stops 21:00 IST · last updated 10:42 IST                              │
│   Markets        ├────────────────────────────────────────────────────────────────────────────────────────┤
│   Research       │ Trade › Command Center               [New trade setup]   [Manage]  Accounting  Reports │
│ ▶ Trade          │ ┌─ Daily loss used ────────┐ ┌─ Buys today ─────────────┐ ┌─ Day P&L (paper book) ───┐ │
│    Command       │ │ ₹0 of ₹5,000             │ │ 3 of 10                  │ │ ▲ +₹1,240 gross          │ │
│    Explorer      │ │ ░░░░░░░░░░ 0 %           │ │ ███░░░░░░░ 30 %          │ │ ▲ +₹1,032 actual         │ │
│    Setup         │ └──────────────────────────┘ └──────────────────────────┘ └──────────────────────────┘ │
│    Monitor       │ ┌─ Waiting for you · 2 ──────────────────┐ ┌─ Top signals (EOD 31 Oct vs live) ─────┐  │
│    Journal       │ │ BUY 6 TCS @ ₹3,912.40 · S3             │ │ TCS BUY 81 · ₹3,904 → ₹3,918 ▲ +0.4 %  │  │
│    Trading       │ │               [Reject] [Confirm & send]│ │ ITC BUY 78 · ₹430.6 → ₹432.1 ▲ +0.4 %  │  │
│    Baskets       │ │                                        │ │                                        │  │
│    Exec. lab     │ └────────────────────────────────────────┘ └────────────────────────────────────────┘  │
│   Sessions       │ ┌─ Sessions · gross vs actual P&L ───────────────────────────────────────────────────┐ │
│   Accounting     │ │ S1 CONFIRM RUNNING  gross ▲ +₹750    actual ▲ +₹542    cycle 2/500 ██░░            │ │
│   Reports        │ │ S3 AUTO    RUNNING  gross ▲ +₹1,412  actual ▲ +₹1,210  cycle 4/500 █░░░            │ │
│   Strategy Lab   │ │ AUTO send: [Ask me first | Send without asking] PAPER only     [All sessions →]    │ │
│   Wealth         │ └────────────────────────────────────────────────────────────────────────────────────┘ │
│   Settings       │ ┌─ Recent order activity ────────────────────────────────────────────────────────────┐ │
│                  │ │ 10:31  ENTRY BUY 6 TCS · trigger ₹3,910 · filled ₹3,912.40 · FILLED · S3           │ │
│ ‹ Collapse       │ └────────────────────────────────────────────────────────────────────────────────────┘ │
└──────────────────┴────────────────────────────────────────────────────────────────────────────────────────┘
```

Trade Setup keeps its ₹ risk preview and adds the chosen session's cash and minimum. Live Monitor adds a cycle-progress bar for each session.

### 4.5 Sessions (`/sessions`)
Primary job: see the 10 sessions as mini-funds and manage each one's money.

```text
┌──────────────────┬────────────────────────────────────────────────────────────────────────────────────────┐
│ ▣ ATIP           │ [Search stocks, pages, commands…  Ctrl K]  [PAPER]  Alerts 2  [■ Halt all trading]  SM │
│                  ├────────────────────────────────────────────────────────────────────────────────────────┤
│   Home           │ ● ATIP running · stops 21:00 IST · last updated 10:42 IST                              │
│   Markets        ├────────────────────────────────────────────────────────────────────────────────────────┤
│   Research       │ Sessions › Overview                   [Split a total…]   [Manage]  Accounting  Reports │
│   Trade          │ Allocated ₹2,50,000 · unallocated ₹0 · 7 running · 1 paused · 1 waiting · 1 off        │
│ ▶ Sessions       │ ┌─ S1 CONF ────┐ ┌─ S2 CONF ────┐ ┌─ S3 AUTO ────┐ ┌─ S4 MAN ─────┐ ┌─ S5 CONF ────┐   │
│    Overview      │ │ Momentum     │ │ Buy zone     │ │ Recovery     │ │ Breakout     │ │ VPI top      │   │
│    S1 … S10      │ │ RUNNING      │ │ RUNNING      │ │ RUNNING      │ │ PAUSED       │ │ RUNNING      │   │
│    Activity      │ │ cycle 2/500  │ │ cycle 1/500  │ │ cycle 4/500  │ │ cycle 0/500  │ │ cycle 1/500  │   │
│   Accounting     │ │ ₹25,677.73   │ │ in RELIANCE  │ │ ₹26,410.20   │ │ ₹25,000.00   │ │ ₹25,120.40   │   │
│   Reports        │ │ min ₹12,000  │ │ min ₹12,000  │ │ min ↑₹13,205 │ │ min ₹12,000  │ │ min ₹12,000  │   │
│   Strategy Lab   │ │ G ▲ +₹750    │ │ G ▼ −₹130    │ │ G ▲ +₹1,412  │ │ G · ₹0       │ │ G ▲ +₹130    │   │
│   Wealth         │ │ A ▲ +₹542    │ │ A ▼ −₹202    │ │ A ▲ +₹1,210  │ │ A · ₹0       │ │ A ▲ +₹88     │   │
│   Settings       │ │ [Setup] [⋯]  │ │ [Setup] [⋯]  │ │ [Setup] [⋯]  │ │ [Setup] [⋯]  │ │ [Setup] [⋯]  │   │
│                  │ └──────────────┘ └──────────────┘ └──────────────┘ └──────────────┘ └──────────────┘   │
│                  │ … S6–S10 on the second row. S9: "Waiting: below minimum (₹11,400 of ₹12,000)"          │
│                  │ ┌─ Compare all 10 · actual P&L (bars), gross (ticks) ────────────────────────────────┐ │
│                  │ │ S1 ▇▇▇▏  S2 ▏(−)  S3 ▇▇▇▇▇▇▇▏  S4 ·  S5 ▇▏  S6 ▇▇▏  S7 ▏(−)  …     [Gross | Actual]│ │
│ ‹ Collapse       │ └────────────────────────────────────────────────────────────────────────────────────┘ │
└──────────────────┴────────────────────────────────────────────────────────────────────────────────────────┘
```

**Session detail with the setup drawer (`/sessions/S1`).**

```text
┌──────────────────┬────────────────────────────────────────────────────────────────────────────────────────┐
│ ▣ ATIP           │ [Search stocks, pages, commands…  Ctrl K]  [PAPER]  Alerts 2  [■ Halt all trading]  SM │
│                  ├────────────────────────────────────────────────────────────────────────────────────────┤
│   Home           │ ● ATIP running · stops 21:00 IST · last updated 10:42 IST                              │
│   Markets        ├────────────────────────────────────────────────────────────────────────────────────────┤
│   Research       │ Sessions › S1 Momentum    [Setup] [Move money] [Pause]   [Manage]  Accounting  Reports │
│   Trade          │ CONFIRM · RUNNING · cycle 2 of 500 · auto-pick on · since 30 Oct 2026 · VIRTUAL        │
│ ▶ Sessions       │ ┌─ Start ──────┐ ┌─ Gross P&L ──┐ ┌─ Charges ────┐ ┌─ Tax (est.) ─┐ ┌─ Actual P&L ─┐   │
│    Overview      │ │ ₹25,000.00   │ │ ▲ +₹750.00   │ │ ▼ −₹72.27    │ │ ▼ −₹135.55   │ │ ▲ +₹542.18   │   │
│    S1 … S10      │ └──────────────┘ └──────────────┘ └──────────────┘ └──────────────┘ └──────────────┘   │
│    Activity      │ ┌─ Gross → actual (waterfall) ───────────┐ ┌─ Cycle-start staircase ────────────────┐  │
│   Accounting     │ │ Gross       ████████████   +750.00     │ │ ₹25,678 ┤          ┌──── cycle 2       │  │
│   Reports        │ │ Charges             ▕█     −72.27      │ │ ₹25,000 ┤ ─────────┘     cycle 1       │  │
│   Strategy Lab   │ │ Tax (est.)        ▕██      −135.55     │ │ minimum ₹12,000 (FIXED) ┄┄┄┄┄┄┄┄       │  │
│   Wealth         │ │ Actual      ████████▏      +542.18     │ │ equity line: gross ──  actual ┄┄       │  │
│   Settings       │ │                                        │ │                                        │  │
│                  │ └────────────────────────────────────────┘ └────────────────────────────────────────┘  │
│                  │ ┌─ Cycles (every column of brief §4.3; the table scrolls sideways) ──────────────────┐ │
│                  │ │ #  Stock  Start ₹    Buy ₹      Sell ₹     Gross    Charges Tax est. Actual  Exit  │ │
│                  │ │ 1  TCS    25,000.00  25,000.00  25,750.00  +750.00  −72.27  −135.55  +542.18 Target│ │
│                  │ │ 2  —      25,677.73  next cycle · auto-pick waits for an ATIP BUY signal           │ │
│                  │ └────────────────────────────────────────────────────────────────────────────────────┘ │
│ ‹ Collapse       │ Activity: 30 Oct POOL ₹25,000 · 31 Oct EDIT trail 1 % · … (from session_events)        │
└──────────────────┴────────────────────────────────────────────────────────────────────────────────────────┘
```

Once a cycle buys, its row shows the whole-share quantity and the "cash left idle this cycle". The Setup drawer opens from the right on desktop and as a full-screen sheet on a phone. It holds:
1. Name, strategy, universe, mode and status.
2. Starting amount (₹). It is locked while a trade is open, with the reason and a link to that trade.
3. Minimum per cycle (₹), with its growth rule: FIXED, RATCHET_PCT (X %) or STEP (₹Y every N profitable cycles). A live line shows the result, for example "next cycle needs ₹13,205; S3 has ₹26,410".
4. Exit rules: target, stop, trail and hold days.
5. Max cycles and auto-pick.
6. Compounding basis: CASH_AFTER_CHARGES or AFTER_TAX_RESERVE.

Save opens a confirmation that shows the ₹ impact, for example "S3 restarts its books from now with ₹40,000. Unallocated falls from ₹15,000 to ₹0." The help panel works through the brief's §4.6 example.

### 4.6 Accounting (`/accounting`, shown on Tax register)
Primary job: account for every rupee, with paper (virtual) and broker (actual) money kept separate.

```text
┌──────────────────┬────────────────────────────────────────────────────────────────────────────────────────┐
│ ▣ ATIP           │ [Search stocks, pages, commands…  Ctrl K]  [PAPER]  Alerts 2  [■ Halt all trading]  SM │
│                  ├────────────────────────────────────────────────────────────────────────────────────────┤
│   Home           │ ● ATIP running · stops 21:00 IST · last updated 10:42 IST                              │
│   Markets        ├────────────────────────────────────────────────────────────────────────────────────────┤
│   Research       │ Accounting › Tax register      [FY 2026-27 ▾] [Export]   [Manage]  Accounting  Reports │
│   Trade          │ 1 Apr 2026 – 31 Mar 2027 · to date · ESTIMATE, NOT TAX ADVICE · [VIRTUAL | ACTUAL]     │
│   Sessions       │ ┌─ Estimated liability (est.) ───────────┐ ┌─ Set-off (FY view) ────────────────────┐  │
│ ▶ Accounting     │ │ ₹3,337.38 so far this FY               │ │ Short-term gains   ▲ +₹19,214.40       │  │
│    General ledger│ │ STCG 20 % on ₹16,686.90 net            │ │ Short-term losses  ▼ −₹2,527.50        │  │
│    Session ledger│ │ LTCG 12.5 % · ₹0 of ₹1.25 lakh used    │ │ Net STCG             ₹16,686.90        │  │
│    Charges       │ │ Speculative / business: none           │ │ Carried forward: none                  │  │
│    Tax register  │ │                                        │ │                                        │  │
│    Funds         │ └────────────────────────────────────────┘ └────────────────────────────────────────┘  │
│    Reconcile     │ ┌─ Tax register · by head and session ───────────────────────────────────────────────┐ │
│    Transfers     │ │ Head  Session  Cycles  Gross ₹     Charges ₹   Net ₹       Tax est. ₹              │ │
│   Reports        │ │ STCG  S1       1       +750.00     −72.27      +677.73     135.55                  │ │
│   Strategy Lab   │ │ STCG  S3       4       …           …           …           …                       │ │
│   Wealth         │ │ sticky header · sort · filter · [Columns ▾] · row → its ledger lines in a drawer   │ │
│   Settings       │ └────────────────────────────────────────────────────────────────────────────────────┘ │
│                  │                                                                                        │
│ ‹ Collapse       │                                                                                        │
└──────────────────┴────────────────────────────────────────────────────────────────────────────────────────┘
```

Transfers: from a session (or unallocated) to a session (or unallocated), with an amount and a reason. A transfer is refused while the source session holds an open trade, and the refusal names that trade. Reconciliation shows each difference with "Accept" and "Investigate" actions.

### 4.7 Reports (`/reports`)
Primary job: get the right statement, on schedule, in the right format.

```text
┌──────────────────┬────────────────────────────────────────────────────────────────────────────────────────┐
│ ▣ ATIP           │ [Search stocks, pages, commands…  Ctrl K]  [PAPER]  Alerts 2  [■ Halt all trading]  SM │
│                  ├────────────────────────────────────────────────────────────────────────────────────────┤
│   Home           │ ● ATIP running · stops 21:00 IST · last updated 10:42 IST                              │
│   Markets        ├────────────────────────────────────────────────────────────────────────────────────────┤
│   Research       │ Reports › Catalogue                [New custom report]   [Manage]  Accounting  Reports │
│   Trade          │ ┌─ Daily session summary ──┐ ┌─ Weekly performance ─────┐ ┌─ Monthly P&L statement ──┐ │
│   Sessions       │ │ Trading days, ~17:00 IST │ │ Monday 08:00 IST         │ │ 1st of the month         │ │
│   Accounting     │ │ last: today 16:58        │ │ last: Mon 26 Oct         │ │ last: 1 Nov (October)    │ │
│ ▶ Reports        │ │ [Run now] [Schedule]     │ │ [Run now] [Schedule]     │ │ [Run now] [Schedule]     │ │
│    Catalogue     │ └──────────────────────────┘ └──────────────────────────┘ └──────────────────────────┘ │
│    Schedules     │ ┌─ Quarterly review ───────┐ ┌─ FY tax statement ───────┐ ┌─ Session cycle book ─────┐ │
│    Archive       │ │ Quarter start            │ │ 1 Apr · on demand        │ │ On demand                │ │
│    Custom        │ │ next: 1 Jan 2027         │ │ CA-ready · estimate      │ │ pick a session           │ │
│   Strategy Lab   │ │ [Run now] [Schedule]     │ │ [Run now] [Schedule]     │ │ [Run now]                │ │
│   Wealth         │ └──────────────────────────┘ └──────────────────────────┘ └──────────────────────────┘ │
│   Settings       │ ┌─ Archive (newest first) ───────────────────────────────────────────────────────────┐ │
│                  │ │ Made (IST)   Report                 Period      Env      Download                  │ │
│                  │ │ 2 Nov 16:58  Daily session summary  2 Nov 2026  VIRTUAL  [PDF] [XLSX] [CSV] [HTML] │ │
│                  │ │ 1 Nov 08:00  Monthly P&L statement  Oct 2026    VIRTUAL  [PDF] [XLSX] [CSV] [HTML] │ │
│ ‹ Collapse       │ └────────────────────────────────────────────────────────────────────────────────────┘ │
└──────────────────┴────────────────────────────────────────────────────────────────────────────────────────┘
```

### 4.8 Strategy Lab (`/strategies`)
Primary job: test an idea and promote the winner to a session in one flow.

```text
┌──────────────────┬────────────────────────────────────────────────────────────────────────────────────────┐
│ ▣ ATIP           │ [Search stocks, pages, commands…  Ctrl K]  [PAPER]  Alerts 2  [■ Halt all trading]  SM │
│                  ├────────────────────────────────────────────────────────────────────────────────────────┤
│   Home           │ ● ATIP running · stops 21:00 IST · last updated 10:42 IST                              │
│   Markets        ├────────────────────────────────────────────────────────────────────────────────────────┤
│   Research       │ Strategy Lab › Run → compare → promote       [New run]   [Manage]  Accounting  Reports │
│   Trade          │ ① Run ──────────────── ② Compare ──────────────── ③ Promote to a session               │
│   Sessions       │ ┌─ Runs ─────────────────────────────────┐ ┌─ Compare (same period, same costs) ────┐  │
│   Accounting     │ │ □ Momentum v3   CAGR 18 % · DD −9 %    │ │               v4          Recovery v2  │  │
│   Reports        │ │ ■ Momentum v4   CAGR 21 % · DD −11 %   │ │ Net CAGR      21 %        15 %         │  │
│ ▶ Strategy Lab   │ │ ■ Recovery v2   CAGR 15 % · DD −6 %    │ │ Max drawdown  −11 %       −6 %         │  │
│    Strategies    │ │ [Compare 2 selected →]                 │ │ Charges       ₹9,120      ₹6,480       │  │
│    Builder       │ │                                        │ │                                        │  │
│    Backtests     │ └────────────────────────────────────────┘ └────────────────────────────────────────┘  │
│    Quant         │ ┌─ Promote ──────────────────────────────────────────────────────────────────────────┐ │
│    ML            │ │ Promote Momentum v4 to [S4 ▾] as its strategy. Mode stays MANUAL till you change it│ │
│   Wealth         │ │ Shows S4's starting amount, minimum and exit rules that will apply.       [Promote]│ │
│   Settings       │ └────────────────────────────────────────────────────────────────────────────────────┘ │
│                  │                                                                                        │
│ ‹ Collapse       │                                                                                        │
└──────────────────┴────────────────────────────────────────────────────────────────────────────────────────┘
```

### 4.9 Wealth (`/wealth`)
Primary job: track long-term net worth and goals. This is separate from trading.

```text
┌──────────────────┬────────────────────────────────────────────────────────────────────────────────────────┐
│ ▣ ATIP           │ [Search stocks, pages, commands…  Ctrl K]  [PAPER]  Alerts 2  [■ Halt all trading]  SM │
│                  ├────────────────────────────────────────────────────────────────────────────────────────┤
│   Home           │ ● ATIP running · stops 21:00 IST · last updated 10:42 IST                              │
│   Markets        ├────────────────────────────────────────────────────────────────────────────────────────┤
│   Research       │ Wealth › Overview                        [Add holding]   [Manage]  Accounting  Reports │
│   Trade          │ ┌─ Net worth ──────────────┐ ┌─ Goals on track ─────────┐ ┌─ Allocation drift ───────┐ │
│   Sessions       │ │ ₹##,##,###               │ │ 3 of 4                   │ │ Equity 4 % over target   │ │
│   Accounting     │ │ ▲ +x.x % this year       │ │ Retirement 72 %          │ │ [Plan rebalance]         │ │
│   Reports        │ └──────────────────────────┘ └──────────────────────────┘ └──────────────────────────┘ │
│   Strategy Lab   │ ┌─ Allocation (actual vs target) ────────┐ ┌─ Goals ────────────────────────────────┐  │
│ ▶ Wealth         │ │ Equity  ███████▏ 64 % (target 60 %)    │ │ Retirement 2045   ███████░░ 72 %       │  │
│    Overview      │ │ Debt    ███▏     26 % (target 30 %)    │ │ Education 2032    █████░░░░ 55 %       │  │
│    Wealth        │ │ Gold    █▏       10 % (target 10 %)    │ │ [All goals →]                          │  │
│    Goals         │ │                                        │ │                                        │  │
│    Allocation    │ └────────────────────────────────────────┘ └────────────────────────────────────────┘  │
│    Rebalance     │ Rebalance keeps its note that capital-gains tax is outside Wealth's scope; sessions    │
│    Performance   │ have their own tax estimate under Accounting.                                          │
│    Advisor       │                                                                                        │
│   Settings       │                                                                                        │
│                  │                                                                                        │
│ ‹ Collapse       │                                                                                        │
└──────────────────┴────────────────────────────────────────────────────────────────────────────────────────┘
```

### 4.10 Settings & Admin (`/settings`)
Primary job: one place for credentials, limits, tax rates, channels and health.

```text
┌──────────────────┬────────────────────────────────────────────────────────────────────────────────────────┐
│ ▣ ATIP           │ [Search stocks, pages, commands…  Ctrl K]  [PAPER]  Alerts 2  [■ Halt all trading]  SM │
│                  ├────────────────────────────────────────────────────────────────────────────────────────┤
│   Home           │ ● ATIP running · stops 21:00 IST · last updated 10:42 IST                              │
│   Markets        ├────────────────────────────────────────────────────────────────────────────────────────┤
│   Research       │ Settings & Admin › Overview                              [Manage]  Accounting  Reports │
│   Trade          │ ┌─ Credentials ────────────┐ ┌─ Risk limits ────────────┐ ┌─ Tax config (estimate) ──┐ │
│   Sessions       │ │ Dhan ✓ · token 18 h left │ │ Daily loss ₹5,000        │ │ STCG 20 % · LTCG 12.5 %  │ │
│   Accounting     │ │ Zerodha: off             │ │ Buys a day 10            │ │ Slab 30 % · cess 4 %     │ │
│   Reports        │ │ API keys · vault         │ │ Max order ₹1,00,000      │ │ from 1 Apr 2026          │ │
│   Strategy Lab   │ │                          │ │                          │ │                          │ │
│   Wealth         │ └──────────────────────────┘ └──────────────────────────┘ └──────────────────────────┘ │
│ ▶ Settings       │ ┌─ Notifications ──────────┐ ┌─ Operating schedule ─────┐ ┌─ Health ─────────────────┐ │
│    Overview      │ │ Telegram ✓ (Mac only)    │ │ Stop 21:00 · resume 06:00│ │ Pipeline OK · DQS 97     │ │
│    Account       │ │ E-mail: not set          │ │ Override: none           │ │ Dhan feeds ✓             │ │
│    Brokers       │ │ Report delivery →        │ │ [Stop now] [Start now]   │ │ [Open health view]       │ │
│    Admin         │ │                          │ │                          │ │                          │ │
│    Compliance    │ └──────────────────────────┘ └──────────────────────────┘ └──────────────────────────┘ │
│    Privacy       │ Every change needs the dashboard token; risk-limit and tax edits show before and after │
│    Assistant     │ values and are logged. On the VIEWER copy every control here is read-only.             │
│    Health        │                                                                                        │
│                  │                                                                                        │
│ ‹ Collapse       │                                                                                        │
└──────────────────┴────────────────────────────────────────────────────────────────────────────────────────┘
```

### 4.11 Home on a phone (390 px)

```text
┌──────────────────────────────────────────┐
│ ▣ ATIP  [PAPER]  ● Running      [■ Halt] │
│ [Search stocks, pages, commands…]        │
├──────────────────────────────────────────┤
│ Today · Mon 2 Nov · 10:42 IST            │
│ ┌──────────────────────────────────────┐ │
│ │ ✓ SAFE     not halted · feed 6 s     │ │
│ │ ● RUNNING  8 of 10 sessions          │ │
│ │ ▲ MONEY    today +₹1,032 actual      │ │
│ │            (gross +₹1,240)           │ │
│ └──────────────────────────────────────┘ │
│ ┌ Waiting for you · 2 ─────────────────┐ │
│ │ BUY 6 TCS @ ₹3,912.40 · S3           │ │
│ │ [Reject]          [Confirm & send]   │ │
│ └──────────────────────────────────────┘ │
│ ┌ Total P&L · all sessions ────────────┐ │
│ │ Gross  ▲ +₹18,420  Charges −₹1,733   │ │
│ │ Tax est ▼ −₹3,337  Actual ▲ +₹13,350 │ │
│ └──────────────────────────────────────┘ │
│ Alerts (2) ›   Market ›   Pipeline ✓ ›   │
├──────────────────────────────────────────┤
│ [Home]  Trade  Sessions  Reports  More   │
└──────────────────────────────────────────┘
```

### 4.12 Sessions on a phone (390 px)

```text
┌──────────────────────────────────────────┐
│ ▣ ATIP  [PAPER]  ● Running      [■ Halt] │
│ [Search stocks, pages, commands…]        │
├──────────────────────────────────────────┤
│ Sessions · ₹2,50,000 allocated           │
│ [Gross | Actual]          [Split total…] │
│ ┌──────────────────────────────────────┐ │
│ │ S1 Momentum      CONFIRM · RUNNING   │ │
│ │ Actual ▲ +₹542.18  Gross ▲ +₹750.00  │ │
│ │ cycle 2/500 ██░░ · cash ₹25,677.73   │ │
│ │ minimum ₹12,000 FIXED  [Setup] [⋯]   │ │
│ └──────────────────────────────────────┘ │
│ ┌──────────────────────────────────────┐ │
│ │ S2 Buy zone      CONFIRM · RUNNING   │ │
│ │ Actual ▼ −₹202   Gross ▼ −₹130       │ │
│ │ in RELIANCE since 29 Oct · open      │ │
│ └──────────────────────────────────────┘ │
│ ┌──────────────────────────────────────┐ │
│ │ S9 Waiting: below minimum            │ │
│ │ ₹11,400 of ₹12,000         [Setup]   │ │
│ └──────────────────────────────────────┘ │
│ … S3–S10 (cards stack; a card's ⋯ menu   │
│   has Pause · Move money · Cycle book)   │
├──────────────────────────────────────────┤
│ Home  Trade  [Sessions]  Reports  More   │
└──────────────────────────────────────────┘
```

---

## 5. Test

### 5.1 Heuristic review of the current UI (Nielsen's 10)

Severity uses Nielsen's scale: 0 not a problem · 1 cosmetic · 2 minor · 3 major · 4 catastrophe.

| ID | Heuristic | Issue today (where) | Sev | How the new design fixes it |
|---|---|---|---|---|
| HR-01 | 1 Visibility of system status | `/` has no halt state, no environment badge and no session P&L. Its 9 KPI tiles are all market data (`server.py`, Market Health … USD/INR) | 3 | Home's five-second status block; the environment badge and kill-switch state in the shell |
| HR-02 | 1 Visibility of system status | PAPER/LIVE shows only on `/command*`. About 25 other pages never say whether an action is paper or real | 3 | The environment badge, with its word, on every page |
| HR-03 | 1 Visibility of system status | The viewer copy can be up to 10 min old (`viewer_sync`). Only the role banner gives the time | 2 | "VIEWER · data from the Mac at HH:MM (n min ago)" in the shell; amber after 20 min |
| HR-04 | 1 Visibility of system status | `/health` is JSON only. `/compliance` links to `/ops`, which returns 404 (`w38_page.py`) | 2 | Settings › Health renders the `/health` payload as a page; the `/ops` link moves there |
| HR-05 | 2 Match with the real world | "Occurrence" (`/command/pnl`) vs the owner's "cycle". "P&L" means three things: Journal's net P&L, `/command/pnl`'s change in equity, and "Booked" | 2 | One glossary: cycle; Gross P&L, Net before tax, Actual P&L, each labelled |
| HR-06 | 2 Match with the real world | Errors show code names: "min_buy_value Rs 30,000 is more than the Rs 25,000 capital", "capital must be positive" (`orders/sessions.py _pool_values`) | 2 | Plain copy: "Minimum ₹30,000 is more than the ₹25,000 starting amount." |
| HR-07 | 2 Match with the real world | "P&L" everywhere is net of charges with no tax and no label. The owner reads it as what he earned | 3 | Gross and actual shown side by side and never blended; tax marked "est." |
| HR-08 | 3 User control and freedom | 16 page files move around with a "← Dashboard" link; only the research pages also link to each other. No breadcrumb | 3 | Persistent rail, breadcrumb, Ctrl-K |
| HR-09 | 3 User control and freedom | The kill switch exists only on `/command` and its sub-pages. From any other page, stopping takes 4 clicks | 4 | Kill switch in the shell top bar: 2 clicks from anywhere, phone included |
| HR-10 | 3 User control and freedom | Restarting a pool resets its books (`start_pool` sets `pool_started_at`). Nothing previews what restarts | 3 | The Setup confirmation states "restarts its books from now" and shows the ₹ change |
| HR-11 | 4 Consistency and standards | Each page has its own palette and navigation: tokens and side nav on `/command`, 15 flat links and 13 tabs on `/`, `_TOP` links on `/account`. Only `command_page.py` and `global_asia.py` honour light mode | 2 | One shell, one token set from `/static/`, light and dark themes |
| HR-12 | 4 Consistency and standards | Tables differ page to page. CSV export exists on only a few (brokers, research, `/app`) | 2 | A shared table: sticky header, sort, filter, column chooser, CSV, row → drawer |
| HR-13 | 5 Error prevention | The "Start the sessions" confirmation on `/command/pnl` doesn't say that sessions beyond total ÷ per-session amount lose their pools (`setup_pools` ends them) | 3 | "Split a total" only pre-fills the cards. Each change is reviewed per session |
| HR-14 | 5 Error prevention | Write buttons stay enabled while STOPPED (423) and on the viewer (403). The owner learns this only from an `alert()` after clicking | 3 | Shell controls are disabled with the reason. Older page bodies still need marking (see appendix) |
| HR-15 | 5 Error prevention | The UI can give every session only the same amount. Different amounts mean hand-typed CLI or API calls with no form checks | 3 | A Setup drawer for each session, with checks |
| HR-16 | 6 Recognition over recall | The owner must remember CLI flags (`--pools 40000 --ids S3`) and config keys ("add max_daily_loss_value in risk_limits" on `/command`) | 3 | Settings groups risk limits, tax configuration and channels as forms |
| HR-17 | 6 Recognition over recall | `/baskets` and `/app` are linked from no page. `/research`, `/screener` and `/market-pulse` link only to each other. 11 pages are missing from the `/` top bar | 3 | All pages in the rail, plus search |
| HR-18 | 7 Flexibility and efficiency | No global search and no keyboard shortcuts. Finding a score means `/` → tab → filter | 2 | Ctrl/Cmd-K now; go-to keys (`g h`, `g s`) and `/` to focus a table filter later |
| HR-19 | 8 Aesthetic and minimalist | `/` stacks 15 links, 13 tabs, 9 KPI tiles and five banners (run state, stale, health, broker, pending) above the content | 2 | `/home` shows status first; banners merge into the shell, one line each |
| HR-20 | 8 Aesthetic and minimalist (and WCAG) | 11 px `#64748b` text on `#1e293b` is about 3.1:1, used about 60 times in `server.py`. The `/` tabs are clickable `div`s with no focus style | 3 | Tokens at 4.5:1 or better; real `button`/`role="tab"`; visible focus ring |
| HR-21 | 9 Recover from errors | Errors arrive in `alert()` with raw text ("HTTP 423", or the API message). "Holds an open trade" gives no link to that trade | 2 | Errors appear inline next to the control, in plain words, with a link to the cause |
| HR-22 | 9 Recover from errors | When a confirmation expires, it disappears from the waiting list without a word | 2 | The row stays as "Expired at 10:55, not sent", with "Set up again" |
| HR-23 | 10 Help and documentation | Exit rules are explained in a long paragraph inside a collapsed `<details>` on `/command/pnl`. Tax and charges are explained nowhere | 2 | Inline help in the Setup drawer; the §4.6 worked example on screen |
| HR-24 | 10 Help and documentation | The acronyms VPI, ZPI, RRI, MRI, CRI and ACS have no glossary on `/` | 1 | A hover/focus glossary on column headers (see appendix) |

### 5.2 Cognitive walkthrough of the top 10 tasks

The four questions:
- **Q1** Will the owner try to achieve the right effect?
- **Q2** Will he notice the correct action?
- **Q3** Will he link that action to the effect he wants?
- **Q4** Will he see that it worked?

The walkthrough was run on the prototype in §4. Each failure found was fixed in the design; the fix is recorded as a WT item.

**T1 · Is ATIP safe and making money today?** Today it takes 3 clicks across `/`, `/command` and `/command/pnl`, and the answer has no tax.
- Q1 Yes. He comes to Home for it.
- Q2 Yes. The status block is first, and the top bar already shows the badge, run state and halt state.
- Q3 Yes. The three lines are labelled SAFE, RUNNING and MONEY.
- Q4 Yes. It refreshes every 15 s and shows its time.
- **Failure:** on a day with no closed cycle, the draft showed "₹0", which reads as "not making money". Gross could be positive while actual was negative. **Fix (WT-01):** show today and since-start together, "No cycle closed today" when nothing closed, and lead with actual.

**T2 · Give S3 ₹40,000 and a ratcheting minimum.** Today: CLI only, and minimum rules do not exist.
- Q1 Yes.
- Q2 Yes. Every card has a Setup button, and Ctrl-K "S3 setup" also opens it.
- Q3 Partly. "RATCHET_PCT" means nothing to him on its own.
- Q4 Yes. A confirmation with the ₹ impact, then an entry in the activity timeline.
- **Failures:** (a) the draft let him type a new amount while S3 held a trade, and refused it only on Save; (b) it was unclear where the extra ₹15,000 comes from. **Fixes:** WT-02: the amount field is locked with "S3 holds TCS (bought 2 Nov); change the amount after it exits", while the minimum and exit rules stay editable. WT-03: the rule names become plain choices ("Fixed", "Rises with the pool: X % of the biggest cycle start", "Steps up after wins") with a live preview, and the confirmation shows unallocated before and after.

**T3 · What did S1 really earn after charges and tax?** Today: 4 clicks, and the figure is net of charges only.
- Q1 Yes.
- Q2 Yes. The Sessions cards show G and A.
- Q3 Partly. "A" alone was cryptic.
- Q4 Yes. The waterfall on the detail page goes gross → charges → tax (est.) → actual.
- **Failure:** an open trade's unrealised gain was neither in the totals nor mentioned. **Fix (WT-04):** card labels read "Actual (after charges & tax est.)", with a line "Open trade: ▲ +₹x unrealised, not in these totals".

**T4 · Stop everything now.** Today: 4 clicks, and only from `/command*`.
- Q1 Yes.
- Q2 Yes. The red button is in the top bar of every page.
- Q3 Yes. "Halt all trading".
- Q4 Yes. A red banner on every page, and "n sessions paused".
- **Failures:** on the viewer the button would fail with 403. Resuming used a browser `confirm()`. **Fix (WT-05):** the viewer shows a disabled "Halt runs on the Mac". Resume becomes a proper dialog that says sessions stay paused until each one is resumed.

**T5 · Download last month's P&L statement as PDF.** Today it can't be done; the nearest is printing `/command/pnl`.
- Q1 Yes.
- Q2 Yes. The archive sits on the Reports landing, newest first.
- Q3 Yes. Period and format columns.
- Q4 Partly. PDF generation takes seconds and showed no progress.
- **Fix (WT-06):** "Generating PDF…" with a spinner. The archive row appears when it is done. The catalogue card has a "Last month" preset if the scheduled run is missing.

**T6 · What tax do I owe for FY 2026-27?** Today: no tax anywhere.
- Q1 Yes.
- Q2 Yes. Accounting › Tax register.
- Q3 Partly. The draft opened on gains by head, but he asked "what do I owe".
- Q4 Yes.
- **Fix (WT-07):** lead with "Estimated liability ₹x so far this FY (estimate, not tax advice)", then set-offs, the LTCG exemption used, and gains by head. The FY selector defaults to the current FY (1 Apr 2026 – 31 Mar 2027, "to date").

**T7 · Move ₹5,000 from S2 to S4.** Today it can't be done; sessions do not lend to each other.
- Q1 Yes.
- Q2 No, in the first draft. Transfers lived only under Accounting, and he looked on S2's card.
- Q3 Yes.
- Q4 Yes. Both balances, before and after.
- **Fix (WT-08):** "Move money" in each card's ⋯ menu and on the session page, pre-filled with the source. If S2 holds a trade, the move is refused with "S2 holds RELIANCE; move money after it exits", and Accounting › Transfers offers the same form.

**T8 · Check a pending confirmation.** Today: 2 clicks, without the session or stop shown.
- Q1 Yes.
- Q2 Yes. The Alerts badge, and "Waiting for you" on Home and Trade.
- Q3 Yes. "Confirm & send" / "Reject".
- Q4 Yes. The row moves to recent activity with its status.
- **Failure:** confirming from the alerts panel hid the signal-vs-live gap. **Fix (WT-09):** the panel row shows the session, stop, expiry and gap. It uses the same 409 slippage re-check ("Price moved 1.4 %. Place anyway at ₹3,968?").

**T9 · Find a stock's ATIP score.** Today: 2 clicks, and only from `/`.
- Q1 Yes.
- Q2 Partly. Ctrl-K is invisible to anyone who doesn't know it.
- Q3 Yes.
- Q4 Yes. The drawer shows the score with its EOD date.
- **Fix (WT-10):** the search field is always visible with a "Ctrl K" hint; the phone gets a search icon. Results are grouped Stocks / Pages / Commands.

**T10 · Schedule the weekly report.** Today it can't be done: `/app` is unlinked and has no session report.
- Q1 Yes.
- Q2 Yes. A Schedule button on each catalogue card.
- Q3 Partly. "08:00" didn't say whose clock, and Telegram works only from the Mac.
- Q4 Yes.
- **Fix (WT-11):** times are always labelled IST, with "Next run: Mon 9 Nov 2026 08:00 IST". The channel list shows which channels are set up and that Telegram sends from the Mac. On the viewer, schedules are read-only.

### 5.3 Design-review appendix

_Statuses checked against the build on 2 Nov 2026 (after the Test and Prod runs); “Partly” says what is and is not there._

"Fixed in build" means the item is closed by the 2 Nov build's scope: the shell, the design tokens and the new Home, Sessions, Accounting, Reports and Settings pages. Check each one against the test report (`docs/testing/ATIP_UIUX_Test_Report_<date>.md`) and the screenshots before sign-off. "Open" means it is designed here but not in the build scope, or is waiting on a decision.

| ID | Issue | Heuristic | Sev | Fix | Status |
|---|---|---|---|---|---|
| HR-01 | `/` lacks halt state, environment and session P&L | 1 Visibility | 3 | Home five-second status; shell badge and halt state | Fixed in build |
| HR-02 | PAPER/LIVE shown only on `/command*` | 1 Visibility | 3 | Environment badge with its word in the shell | Fixed in build |
| HR-03 | Viewer data age unclear | 1 Visibility | 2 | "data from the Mac at HH:MM" in the shell; amber after 20 min | Partly: the existing role banner shows the Mac's data time on every page; no amber-after-20-min yet |
| HR-04 | `/health` is JSON only; `/compliance` → `/ops` returns 404 | 1 Visibility | 2 | Settings › Health page over the `/health` payload (JSON route unchanged); relink `/ops` | Fixed in build: /ops renders /health, /health/ready and job runs; /compliance's link now resolves |
| HR-05 | "Occurrence" vs "cycle"; three different "P&L"s | 2 Real world | 2 | Glossary and labels: cycle, Gross, Net before tax, Actual | Fixed in build |
| HR-06 | Errors show code field names | 2 Real world | 2 | Plain-English validation in the Setup drawer | Fixed in build |
| HR-07 | P&L is net of charges, with no tax and no label | 2 Real world | 3 | Gross and actual side by side; tax "est." | Fixed in build |
| HR-08 | "← Dashboard" is the only way back; no breadcrumb | 3 Control | 3 | Rail, breadcrumb, Ctrl-K | Fixed in build |
| HR-09 | Kill switch only on `/command*` | 3 Control | 4 | Kill switch in the shell top bar, every page | Fixed in build |
| HR-10 | Pool restart resets books without a preview | 3 Control | 3 | Confirmation states the restart and the ₹ change | Fixed in build |
| HR-11 | Per-page palettes; light mode on 2 pages only | 4 Consistency | 2 | Shared `/static/` tokens and shell; both themes | Partly: one token set and shell on every page; older page bodies keep their own palettes |
| HR-12 | Inconsistent tables; little CSV export | 4 Consistency | 2 | Shared table component on new pages; older page tables still to move over | Open |
| HR-13 | "Start the sessions" doesn't warn that pools end | 5 Error prevention | 3 | Split helper only pre-fills; review per session | Fixed in build |
| HR-14 | Write buttons live while STOPPED or on the viewer | 5 Error prevention | 3 | Shell controls disabled with the reason; buttons inside older page bodies still need a `data-write` marker | Open |
| HR-15 | Same capital for all sessions in the UI | 5 Error prevention | 3 | Setup drawer for each session | Fixed in build |
| HR-16 | CLI flags and config keys to remember | 6 Recognition | 3 | Settings forms for limits, tax and channels | Partly: tax rates are a form; risk limits are shown read-only with where to change them |
| HR-17 | Orphan pages (`/baskets`, `/app`); 11 pages missing from the `/` top bar | 6 Recognition | 3 | Every page in the rail and in search | Fixed in build |
| HR-18 | No search or shortcuts | 7 Flexibility | 2 | Ctrl/Cmd-K palette (other go-to keys open) | Fixed in build: Ctrl/⌘K and /, plus g-h/s/t/a/r/c/m/l/w/e go-to keys and ? for help |
| HR-19 | `/` clutter: links, tabs, tiles, five banners | 8 Minimalist | 2 | `/home` status-first; banners merged into the shell | Partly: /home leads with the three answers; the older run-state and token banners still show under the shell |
| HR-20 | 11 px text at about 3.1:1; `div` tabs with no focus | 8 Minimalist / WCAG | 3 | Tokens at 4.5:1 or better and real tabs on new pages; `/` body still to restyle | Open |
| HR-21 | `alert()` errors with raw text | 9 Errors | 2 | Inline errors with the cause linked (new pages); older pages keep `alert()` | Open |
| HR-22 | Expired confirmations vanish silently | 9 Errors | 2 | "Expired at HH:MM, not sent" row | Open: not built in this delivery |
| HR-23 | Exit rules, charges and tax unexplained | 10 Help | 2 | Inline help and the §4.6 worked example | Fixed in build |
| HR-24 | No glossary for VPI, ZPI, RRI, MRI, CRI, ACS | 10 Help | 1 | Header glossary on hover/focus | Open |
| WT-01 | "₹0 today" reads as losing; gross and actual could disagree | 1 Visibility | 3 | Today plus since-start; "No cycle closed today"; actual leads | Partly: today's actual leads with gross, charges and tax beside it; since-start is in the Sessions widget |
| WT-02 | Amount editable while a trade is open; refused only on Save | 5 Error prevention | 3 | Field locked with the reason and a link to the trade | Fixed in build: the field is locked with the reason (no link to the trade yet) |
| WT-03 | Minimum rule names unclear; source of extra money unclear | 2 Real world | 2 | Plain choices with a live preview; unallocated before and after | Partly: plain choices with help text; no live floor preview |
| WT-04 | Unrealised P&L of an open trade unmentioned | 1 Visibility | 2 | "Open trade … not in these totals" line | Fixed in build |
| WT-05 | Halt on the viewer fails; resume via `confirm()` | 5 Error prevention | 3 | "Halt runs on the Mac"; resume dialog | Partly: the shell's kill switch reads “View only” on the viewer; resume stays on Command Center |
| WT-06 | No progress while a PDF generates | 1 Visibility | 2 | "Generating PDF…", then the archive row | Fixed in build |
| WT-07 | Tax page answers "gains", not "what I owe" | 2 Real world | 2 | Lead with estimated liability; current FY by default | Partly: liability is in the card under the heads table, not first |
| WT-08 | Move money not found on the session card | 6 Recognition | 3 | "Move money" on each card and session page | Partly: Ctrl/⌘K “Move money between sessions” and Accounting › Transfers; no button on each card |
| WT-09 | Alerts-panel confirm hides the gap | 5 Error prevention | 3 | Gap, stop and expiry shown; same 409 re-check | Open: the alerts panel was not changed |
| WT-10 | Ctrl-K not discoverable | 6 Recognition | 2 | Visible search field with hint; phone search icon | Fixed in build |
| WT-11 | Schedule time zone and channel unclear | 2 Real world | 2 | IST labels, "Next run …", channel status | Partly: IST on every schedule; no “next run” or channel status |
| WT-12 | Should STT reduce the taxable gain? The §4.6 example taxes net after all charges, including STT; under s.48 STT is not deductible | 2 Real world | 2 | `accounting/tax.py` exposes `stt_deductible`; the owner or CA chooses the default (see §6) | Open |

---

## 6. Assumptions and open questions

**Assumptions**
- **`funds/report.py` is missing.** The brief cites it, but it does not exist in this checkout or in git history. The Funds view is designed on `portfolio/pnl.py` (PAPER/LIVE books), `portfolio/paper_report.py` and the session pools.
- **The current UI differs slightly from the brief's counts.** The `/` top bar has 15 links and 13 tabs, including Live (the brief says 17 and 12). The phone apps have 4 tabs: Today, Live, Command, Global & Asia (the brief says 3). There is a `/command/pnl` page the brief does not list.
- **`/` stays the ATIP Scores page and `/home` is the new landing page.** No old URL redirects away from its content.
- **The viewer persona covers two kinds of reader:** the owner away from the Mac, and a trusted reader. ATIP has one owner, and the viewer holds no token.
- **The rules for counting clicks are in §3.2.** Counts for "Today" were walked through the current pages from `/screener`.
- **Wireframe figures are illustrative.** S1's numbers follow the brief's §4.6 example (₹750.00 gross, ₹72.27 charges, ₹135.55 tax est., ₹542.18 actual). The other figures are made up but add up consistently.
- **`docs/testing/screenshots/` does not exist yet.** It is produced by the build's Playwright run. Appendix statuses describe the build's scope and must be checked against it.

**Open questions for the owner**
1. **STT and tax.** Should the FY statement treat STT as deductible (it then matches the §4.6 example) or not (s.48)? See WT-12.
2. **Slab rate.** Which slab rate applies to intraday and F&O? The draft configuration uses 30 % plus 4 % cess.
3. **Where a bigger session's money comes from.** In PAPER, what is the "unallocated" pool: total paper cash less the sessions' starting amounts? Is there a cap?
4. **Transfers into a busy session.** May a transfer go into a session that holds an open trade? The brief restricts only the source.
5. **What the viewer may see.** Should the viewer see Accounting › Tax register and Settings, or only Home, Sessions and Reports?
6. **Report delivery.** Telegram sends only from the Mac. Is e-mail delivery wanted for scheduled reports?
7. **Mobile tab order.** Should More list Accounting first, since it is the most-used section after the five tabs?
