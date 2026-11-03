# Merge: cloud session "UI fixes v2" + this Mac's 3 Nov work (3 Nov 2026, IST)

**What was compared.** The cloud session's code is on GitHub:
- `claude/eloquent-hopper-1oou1b`, three commits: UI fixes v2 H1–H10 and G1–G8, the trading workspace W1–W6, and the
  technical-signals catch-up.
- `claude/eloquent-hopper-1oou1b-ci`: the CI fix, already merged into `origin/master` (`8711fc7`).

Both start from GitHub's 2 Nov master (`234274d`). That session could not reach this Mac, as its own notes say. So it
never saw the Mac's 3 Nov commits: the UX audit fixes, the recheck, and the ten-session + score audit. Its "Open
items 1" asks for exactly this merge.

**Branch.** `merge/ui-fixes-v2-2026-11-03` = `fix/sessions-score-audit-2026-11-03` + `origin/master` (CI fix, clean)
+ `claude/eloquent-hopper-1oou1b` (19 files in conflict, resolved by hand).

## How each conflict was resolved

| File | Kept from this Mac | Taken from the cloud | Note |
|---|---|---|---|
| `dashboard/shell.py` | SVG icon set, alert bell pop-over, density toggle, the one feed pill, `inert` drawers (no `aria-hidden`) | P&L strip (W5), watchlist rail and toggle (W1), order-ticket drawer (W2), ⋯ overflow menu (G7), breadcrumb that never truncates the section (H8), "P&L by period" in Accounting (H1), tabs Home · Watchlist · Trade · Sessions · More | New `star` icon for the watchlist |
| `dashboard/static/atip/atip.css` | the whole legacy light/dark bridge (UX audit H3), feed pill, bell, density, score pills | every UI-fixes-v2 rule (answers, chips, alerts, disclosures, See also, container queries, overflow) | **Dropped one cloud rule** that hard-set `.pos`/`.neg` to light green/red "on the dark older pages". On this Mac the older pages follow the light theme, where those colours fail contrast |
| `dashboard/static/atip/atip.js` | bell, feed label, drawers with `inert` only | unread counts in the overflow menu; Esc closes an open dialog first | Esc also closes the bell |
| `dashboard/ux_pages.py` | **Trade of the Day only on a BUY signal** (owner decision 3 Nov), the one feed label in "Is ATIP safe?", the ten-session board | the three answers (icon + word + amount), Today / Since-start periods, the Sessions toggle, the Buy and Chart buttons, `/stock/` links | |
| `dashboard/server.py` (`/`) | status strip, theme bridge, order modal (PAPER/LIVE line, charges, "Buy only on BUY"), a11y labels, the feed mark on price columns | a flat move shows grey "• 0.00 (0.00%)" (G1, server and live poller); rank shows "—" when missing; no "None" in the Factor or Trade-of-the-Day text | |
| `dashboard/w39_page.py` | labelled form fields, h2 headings, loading skeletons | the option-chain assets (W6), "ⓘ How this works" disclosures and term tooltips (G4) | |
| `dashboard/stock_view.py`, `execution_page.py` | theme tokens, sectioned loaders | live 52-week stats and collision-aware labels (G6, merged automatically); a darker Buy button for contrast | |
| `mobile/*` | version **2026.11.03.2** (Android 6, iOS 6), the feed text in the native banner | the H10 banner rule (pills only when normal), web tokens, Watchlist tab, dialogs, keyboard resize | **Android had six tabs after the automatic merge** (Live + Watchlist). Android's bottom bar takes five, so the app would have crashed on start. Live was removed from the bar and stays under More |
| `docs/ATIP_MASTER_TRACKER.csv` / `.xlsx` | all rows | all rows (phase 19), renumbered after the Mac's. The cloud's `MOB-07`/`MOB-08` are renamed `V2-MOB-07`/`V2-MOB-08` (the Mac already uses those IDs) | Sheet "Status 2026-11-03 UI v2" added beside "Status 2026-11-03 scores" |
| `tests/test_ui_fixes_v2.py` | — | — | `test_order_paths_are_untouched` now allows the two intended SES-09 files (`orders/autopick.py`, `orders/session_board.py`). UI fixes v2 itself still touches nothing under `orders/` or `execution/` |

## One product decision to confirm

The owner asked for the **Live** tab back on 3 Nov (08:13 IST brief). The later UI fixes v2 brief (13:58 IST) set the
phone tabs to Home · Watchlist · Trade · Sessions · More. A bottom bar holds five, so this merge follows the later
brief: Live is under More, and `/m?tab=live` still works. Swapping Watchlist for Live is one line in each app.
