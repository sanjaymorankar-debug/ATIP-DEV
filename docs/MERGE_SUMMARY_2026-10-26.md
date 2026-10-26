# Merge summary — 26/27 Oct 2026

## Pending UI/UX work found and how it was handled

| Source | What | Decision |
|---|---|---|
| Uncommitted UXR delta (`.ux_build/ATIP_UXR_delta.zip`, applied in the working tree) | UXR-01 Command Center, UXR-02 kill switch UI/API, UXR-03 sessions registry | Reviewed (tests, tenant guard, a11y report, `date.today()` scan). Landed on master as `ccc20b3` (committed on the owner's identity at 23:13 IST while this review ran — identical content). Follow-ups in this branch: IST clock instead of `date.today()` in the halt reason, status banner, Global & Asia link. |
| `deliverables/2026-10-26/ATIP_delta_patches_2026-10-26.zip` | 4 fixes + tracker/reports | Already on master (`1ecda6b`…`5127a78`) — nothing to apply. |
| `origin/claude/zen-meitner-ersxzy` | NSE pre-open capture | Already merged (master is ahead; its diff only deletes newer master code). Not merged again. |
| `origin/w28-signals-news-dashboards` (1 Oct) | early W28 insights page | Superseded by the W28 work on master; merging would delete ~8,200 lines. Not merged. |
| `origin/claude/central-auth-rbac-subscriptions-yity3g` | owner-only gate for a hosted ATIP (bkesari.com login) | Hosting/auth, not UI/UX; depends on the open hosting decision (GAP-2610-16). Not merged — owner decision. |
| `origin/claude/fervent-bell-wcmmdu` | serve under `/ATIP/` base path + Neon PostgreSQL | Hosting, 87 files, diverged; not UI/UX. Not merged — owner decision. |
| Other unmerged remote branches (determined-brahmagupta, nice-gates, charming-maxwell, lucid-faraday, festive-ride, fix-startup-market-catchup, w33-wealth-qa) | PostgreSQL switch, Mac docs, ATR sizing, settings, catch-up, workbooks | No dashboard UI/UX changes. Out of scope. |

No merge conflicts: the UXR commit was already on master; this branch is a fast-forward-able set of 4 commits on it.

## Commits merged into master (`claude/ops-asia-2026-10-26`)

| Commit | One line |
|---|---|
| `34c2804` | Operating schedule: STOPPED 21:00–06:00 IST, 06:00 resume sequence, catch-up, override |
| `529ecf2` | Asia market signals, Global & Asia tab, status banner on every page |
| `cc9a4f6` | Android and iOS apps: Today, Command and Global & Asia with a native status banner |
| `393bb0d` | EMA gap: repair the sessions most short symbols lack first; readable Global & Asia time |
| (last) | Docs, testing report, merge summary, tracker |

## Every change, one line each

**Task 1 — UI/UX**
- `dashboard/command_routes.py`: halt reason uses an IST clock, not `date.today()`.
- `dashboard/command_page.py`: status banner; Global & Asia link in the side nav.
- `dashboard/server.py`: banner above the stale-data banner; "Global & Asia" top-bar link and 12th tab; read-only guard; status/Asia routes; order monitor skips while stopped.
- `dashboard/w38_page.py` (`/m`): banner; Today / Global & Asia / Command screens; PWA cache v3.

**Task 2 — operating schedule**
- `pipeline/operating_schedule.py` (new): window, states, `scheduler_status`/`scheduler_override`/`resume_runs`/`deferred_jobs`, six-step resume, catch-up + lock, override, read-only middleware, `/api/status` payload.
- `pipeline/scheduler.py`: mechanism note; `run_job` refuses in the window; gated loop; start-up and wake ticks; Asia 10:45/17:15 and report 08:15 slots; `news_resume` counts as morning news.
- `orders/risk.py`: the stop window is a third way into `halted()` — every order path refuses.
- `data/dhan_ws.py`: feed window closed while stopped (index, stock and depth feeds).
- `pipeline/premarket_report.py` (new): stored pre-market report with outlook and caveats.
- `data/ema_gap.py` (new): null-EMA gap fill from the Bhavcopy archive.
- `data/news.py`: `GLOBAL_FEEDS` (global, Asia, Pakistan) and a `feeds` parameter.
- `pipeline/health.py`: `news_resume` satisfies "News (morning)".
- `tests/conftest.py`: suite runs with the window off (`ATIP_OPERATING_SCHEDULE=off`).

**Task 3 — Asia markets**
- `data/asia_markets.py`, `data/asia_exchanges.json` (new): sources, verification, storage, sessions, signals, sentiment, Pakistan flag, calendar check.
- `scores/engine.py`: Market Health takes `Asia` when present; `db/schema.py`: MH/Asia weight 0.05 seeded.
- `alerts/telegram.py`: Asia line in the 08:30 brief.
- `dashboard/global_asia.py` (new): `/api/status`, override, `/api/asia/*`, `/api/premarket-report`, `/global-asia`, banner, tab.
- `db/purge.py`: new tables on 600-day / 90-day retention; `enterprise/privacy.py`: classified; `enterprise/authz.py`: override + refresh owner-only.

**Mandatory**
- `mobile/android`, `mobile/ios`, `mobile/README.md` (new): native shells + banner.
- Tracker rows OPS-01..11, ASIA-01..07, MOB-01..02, QA-01; UXR-10/11 updated.
- `docs/OPERATING_SCHEDULE_AND_ASIA_2026-10.md`, `docs/TEST_REPORT_2026-10-26_OPS_ASIA.md`, this file.
