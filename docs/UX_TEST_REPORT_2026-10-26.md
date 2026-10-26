# UX redesign — testing report (26 Oct 2026)

Scope: UXR-01 Command Center (`/command`), UXR-02 kill switch API/UI, UXR-03 session registry.
Code under test: the owner's tree as of commit 5127a78 (incl. R12 per-session tokens) plus this delta. Screenshots: `docs/ux_screens/`.

## Environments

| Environment | Status | Result |
|---|---|---|
| Isolated copy (Linux, Python 3.13, headless Chromium) | Run | See below |
| **Test server** | **Not run — ATIP has no Test server yet** (dev/test on Oracle Cloud is planned) | Blocked (UXR-11) |
| **Prod server** | **Not run — ATIP has no Prod server**; it runs on the owner's Mac | Blocked (UXR-11) |
| Owner's Mac (live data) | Not run from here — needs the owner to start the dashboard | Owner UAT |

## Full regression suite

`python -m pytest` on the whole repository with the delta applied (final run, tree 5127a78):

| Total | Passed | Failed | Skipped |
|---|---|---|---|
| 1,710 | **1,673** | **1** | 36 |

Two failures found by the suite on an earlier run were caused by this change and fixed before the final run:
- `test_no_get_route_shows_the_owner_book_to_another_tenant` — `/api/command/summary` was readable by
  another tenant in enterprise mode. `/api/command`, `/api/sessions`, `/api/risk/halt|resume` added to
  `enterprise/authz.py OWNER_BOOK`, `/command` to `OWNER_PAGES`; the page uses `page_token()` (R12).
- `test_no_sql_uses_a_mysql_reserved_word_unquoted` — `signal` is reserved in MySQL; now back-ticked.

The one remaining failure, `test_block_network_refuses_outside_hosts_and_keeps_loopback`, is
environmental: it fails identically on the untouched original tree in this sandbox (its outbound proxy
answers the request the test expects to be refused). Expected to pass on the Mac.
Skips are the existing ones (live PostgreSQL / MySQL servers, optional packages).

## New tests — 22, all pass

| Case | Type | Result |
|---|---|---|
| /command serves, carries the token and the halt button | Functional | Pass |
| Main dashboard links to /command | Functional | Pass |
| Summary shape on an empty install (10 sessions, engine_enforced false) | Functional | Pass |
| EOD signal paired with live price and gap %; HOLD excluded | Functional | Pass |
| Stale feed flagged only during market hours | Functional | Pass |
| All 6 new POST routes refuse a caller without the token | Security | Pass |
| Halt sets the real kill switch (orders.risk.halted) and pauses running sessions | Safety | Pass |
| Resume refused without `{"confirm":"RESUME"}`; sessions stay paused after resume | Safety | Pass |
| AUTO refused without ack; accepted with ack | Safety | Pass |
| AUTO refused while halted | Safety | Pass |
| LIVE AUTO refused without SEBI Algo ID | Safety | Pass |
| Bad mode / status → 400; unknown session → 404 | Validation | Pass |
| Limits validated; every change in session_events | Audit | Pass |
| Linking a rule counts it on the session | Functional | Pass |
| Seeding is idempotent | Data | Pass |
| Browser, 1366×900: loads clean, 10 tiles, no horizontal scroll, AUTO two-step, halt dialog → banner → flag set | E2E | Pass |
| Browser, 390×844 (phone): same flow | E2E / responsive | Pass |

## Usability walkthrough (task-based, against the prototype and /command)

| Task | Path | Result |
|---|---|---|
| Stop everything now | Halt all trading (header, every screen) → Halt now | 2 clicks; consequence stated before acting |
| Pause session 4 | Session tile S4 → Pause session | 2 clicks |
| Find why RELIANCE is a BUY | Prototype: Signal Explorer → row → "Why this signal" factor bars + plain reason | 2 clicks (UXR-06, not yet built) |
| Bull call spread with ₹5,000 max loss | Prototype: Options Workbench → Bull call spread → max-loss stepper to ₹5,000 → lots computed | 3 clicks (UXR-07 area, not yet built) |

## Accessibility (WCAG 2.1 AA, automated + keyboard)

| Check | Dark | Light |
|---|---|---|
| Text contrast ≥ 4.5:1 (3:1 large) on every visible text node | 0 failures | 0 failures |
| Keyboard: first Tab stop is the halt button | Pass | Pass |
| Halt dialog: Enter opens, focus moves to "Halt now", Escape closes and returns focus | Pass | Pass |
| Gains/losses carry ▲/▼ and a sign, not colour alone | Pass | Pass |
| Controls are real `<button>`s / radios with labels; status via aria-live | Pass | Pass |
| Touch targets ≥ 44 px | Pass | Pass |

## Not tested / open

- Session pause/AUTO are recorded, not enforced by the trigger engine (UXR-04, owner decision).
- Android / iOS apps: none exist; `/command` works at phone width (UXR-10).
- Test and Prod server runs (UXR-11).
- Owner UAT on the Mac with live Dhan data.
