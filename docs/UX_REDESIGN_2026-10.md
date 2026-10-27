# ATIP UX redesign — automatic trading console (Oct 2026)

Prototype (clickable, 8 desktop screens + phone): the "ATIP Trading Console Redesign" design canvas.
Implemented: `/command` (Command Center), the kill-switch API and the session registry (UXR-01..03,
26 Oct); `/command/signals`, `/command/setup`, `/command/monitor`, `/command/journal` and the session gate
in the trigger engine (UXR-06..09, UXR-04, 27 Oct, `dashboard/command_more.py`). Not built: Options
Workbench (the existing /options-builder stands in), Settings & Safety, the phone app screens.

## 1. UX audit of the current dashboard (code read 26 Oct 2026)

**What exists.** `/` (dashboard/server.py, ~1,860 lines of inline HTML) holds a 9-tile KPI row, an
index sidebar, Trade of the Day, sector tiles and 11 tabs (ATIP Scores, Portfolio, Top VPI, Buy Zones,
RRI, MRI, CRI, FII/DII, News, Signal History, Orders) plus the order-rule modal. Around it sit ~30
separate pages (/trading, /execution-lab, /strategies, /strategy-builder, /options-builder, /baskets,
/signals, /screener, /research, /market-pulse, /quant, /ml, /wealth, /brokers, /compliance, /m …),
reached from 13 plain-text links in the top bar. The back end is strong: kill switch
(`orders/risk.py halt()`), pre-trade gateway, confirmation gate with slippage re-check, bracket/OCO/
trailing legs, paper broker, stale-data banner, health panel.

**The signal → trade → exit flow today.** Scores tab → row "Buy/Sell" → modal (side, trigger,
quantity, stop) → rule saved → rule watched every 30 s → "Awaiting confirmation" strip → Confirm →
Orders tab to watch → exit via another rule or the broker. Six screens' worth of context switching,
and the risk of the trade is never shown in rupees before saving.

**Pain points**

| # | Finding | Severity |
|---|---|---|
| A1 | The kill switch has no button. It exists only as `python -m orders.risk --halt` or a flag file; /execution-lab shows its state but cannot set it. | Critical |
| A2 | No single "is it safe / what needs me" view: halt state, broker env, feed age, loss budget and pending confirmations live on different pages. | High |
| A3 | 13 equal-weight top-bar links + 11 tabs; trading, research and admin tools share one level. | High |
| A4 | Trade modal shows no ₹ risk, reward:risk, charges or remaining loss budget before saving. | High |
| A5 | EOD signal vs live price: CMP is live but the signal is from the close; the gap is not flagged. | Medium |
| A6 | No concept of parallel strategies/sessions with their own limits, though 10 are planned. | Medium |
| A7 | Gains/losses are coloured green/red with no arrow or sign in several tables. | Medium (a11y) |
| A8 | Clickable `<div class="tab">` elements, ~11 px grey text on dark (below 4.5:1 in places), no focus styles. | Medium (a11y) |
| A9 | Inline styles everywhere — no tokens, so no light mode and no consistent spacing. | Low |
| A10 | Missing states: market closed is handled for quotes, but broker-disconnected and stale-feed are not surfaced next to the action they block. | Medium |

## 2. Empathise and define

**Persona A — Sanjay, power user.** Runs ATIP on his Mac alongside a day job; checks it before 9:15,
at breaks and after close. Wants ten strategies running in parallel without babysitting each tick, and
wants to know instantly if anything is unsafe. Fear: an automatic order going out on stale data or
after a bad morning.

**Persona B — Beginner-tier user.** Trusts the score, does not know what CRI or IV mean, needs the
suitability gate to stop them, and needs every rupee at risk spelled out.

**Journey map — a trading day**

| Moment | Doing | Needs | Today's friction |
|---|---|---|---|
| 08:30 pre-market | Read report, scan signals | Top signals + market health in one place | Spread over / and /market-pulse |
| 09:15 open | Arm rules, set sessions | Risk preview, session limits | No ₹ risk preview, no sessions |
| Intraday | Confirm triggers, watch | Pending list, distance to stop/target, alerts | Orders tab is a log, not a monitor |
| Something goes wrong | Stop everything | One obvious button | CLI only |
| 15:30 close | Review | Per-session P&L, hit rate | Signal History only, no per-session view |
| Post-market 16:45 | Pipeline runs | Know scores are current | Stale banner exists (good) |

**How might we…**
1. …let him trust an automatic order without watching every tick?
2. …make "stop everything" one click from any screen, and impossible to miss when on?
3. …show the ₹ at risk before a rule is saved, not after it fires?
4. …keep EOD signals and live prices visibly different so neither is mistaken for the other?
5. …run ten strategies with separate limits without ten times the attention?
6. …protect a Beginner from strategies above their tier without slowing the power user?
7. …tell him, honestly, what a 2027 projection is and is not?

## 3. Information architecture and screens

Primary navigation (side menu, 8 items); everything else moves under **More**:

1. **Command Center** — risk budget (loss used, orders, day P&L), market strip, *Waiting for you*,
   top EOD signals with live price and gap, 10 session tiles, what-ATIP-did feed. *(implemented: /command)*
2. **Signal Explorer** — ranked table + "why this signal" factor bars and plain-language story. *(implemented: /command/signals)*
3. **Trade Setup** — side, entry type, steppers for qty/target/stop, live ₹ risk/reward/charges,
   pre-trade checklist (halt, market, funds, circuit, session limit, price gap), session + mode. *(implemented: /command/setup)*
4. **Automation & Sessions** — 10 sessions, mode Manual / Confirm / Auto (Auto is a two-step),
   limits, pause/resume, history. *(registry + UI on /command; the engine holds paused / MANUAL / over-limit entries)*
5. **Live Monitor** — positions with a stop←price→target bar, rules watching, order log with the rule
   behind every action; stale-feed banner. *(implemented: /command/monitor)*
6. **Options Workbench** — strategy picker, payoff chart, size-by-max-loss, Greeks, settlement note.
7. **Journal & Analytics** — per-session trades / successful / win rate / avg R / P&L, a
   straight-line Dec 2027 *range* with an explicit "not a forecast" note, hit rate by score band. *(implemented: /command/journal)*
8. **Settings & Safety** — connections health, account-wide limits, live gate with SEBI Algo ID,
   notification choices.

## 4. Safety-first automation rules (non-negotiable)

- Kill switch on every screen; asks once, states consequences; resume needs an explicit word.
- Daily and per-session loss limits auto-pause; every auto action is logged with rule, price, time,
  order id and result.
- EOD vs live labelled everywhere; stale feed (> 60 s in market hours) puts up a banner.
- Broker disconnected / no funds / market closed block the action and say why, at the button.
- AUTO: second step with an "I understand" checkbox; refused while halted; LIVE AUTO refused
  without a SEBI Algo ID.

## 5. Design system

| Token | Dark (default) | Light | Use |
|---|---|---|---|
| `--bg` | #0B1220 | #F5F7FB | page |
| `--panel` | #111A2C | #FFFFFF | cards |
| `--line` | #22304A | #D5DDEA | borders, meter track |
| `--text` / `--muted` | #E6EBF5 / #9AA8C1 | #0F172A / #4B5873 | ≥ 4.5:1 on panel |
| `--accent` | #38BDF8 | #0369A1 | links, focus ring, selection |
| `--good` / `--bad` | #6EE7B7 / #FCA5A5 | #047857 / #B91C1C | always paired with ▲ / ▼ and a sign |
| `--warn` | #FDE68A on #2A2410 | #854D0E on #FEF6DA | PAPER, pending, gaps |
| `--auto` | #FDBA74 on #3A1F0A | #9A3412 on #FFEEDD | AUTO mode only |
| kill | #DC2626 | #DC2626 | the halt button only |

Type: system sans for UI, monospace with tabular numerals for every price and count. Sizes 12 / 13 /
14 / 15 / 20–22. Spacing 4-based (8, 10, 12, 14, 16, 20). Radius 8 (controls), 10 (cards), 999 (pills).
Touch targets ≥ 44 px. Numbers in `en-IN` (₹1,00,000).

Components: risk meter, P&L chip (arrow + sign + colour), signal pill, session tile (name, mode badge,
status, counts), mode radio card, order-status pill (FILLED / HELD / REFUSED / STOPPED / MOVED),
banner (halt / stale / info), confirm dialog.

Responsive: side menu ≥ 720 px; below that it becomes a horizontal strip, cards stack, wide tables
scroll inside their card, the halt button stays in the header.

## 6. Heuristic evaluation of /command (Nielsen)

| Heuristic | Result |
|---|---|
| Visibility of system status | Market, feed age, signal date, env and halt state in the header; 15 s refresh |
| Match with the real world | ₹, lakh formatting, "Waiting for you", plain-language halt text |
| User control and freedom | Halt is reversible (Resume); AUTO has a "Keep current mode" exit |
| Consistency | One token set, one pill style per meaning |
| Error prevention | AUTO two-step; resume needs confirmation; API refuses AUTO when halted |
| Recognition over recall | Limits shown on the session panel; no CLI needed |
| Flexibility | Power user: 1 screen; deep tools under More |
| Minimalist design | 6 cards, no decorative charts |
| Error recovery | API errors shown inline in plain text |
| Help | Engine-enforcement caveat stated on screen |

## 7. Session enforcement (UXR-04, 27 Oct 2026)

`orders/rules.py check_triggers` asks `orders/sessions.py gate()` before an ENTRY rule linked to a session
fires. It is held (stays ACTIVE, reported as HELD_BY_SESSION and on /command/monitor) while the session is
PAUSED, OFF or MANUAL, or has used its `max_trades` today, lost `max_loss_value` today, or would pass its
`capital_cap`. Bracket exit legs are never held. Rules without a session behave as before.

**Send without asking** (the owner's switch on /command, OFF by default): AUTO alone still asks. Only
with the switch on does an entry rule of a RUNNING AUTO session go straight to the order when it
triggers, through the same path as `require_confirmation=False` (refused on a quote older than 3
minutes, then the halt, funds, circuit and risk-limit checks in orders/broker.py). Turning it on needs the
"I understand" tick, is refused while halted and, for LIVE, without a SEBI Algo ID; every change is
logged in `session_events` (AUTO_SEND). CONFIRM sessions and rules without a session keep asking.

**PAPER only** (owner, 27 Oct 2026): the switch cannot be turned on unless `broker_env` is PAPER, and
whenever it is not PAPER the engine makes every entry ask -- auto send, an AUTO session or a rule's own
`require_confirmation=False` alike. Bracket exit legs keep their own setting, so a stop protecting a
real position still fires without waiting for a tap.
