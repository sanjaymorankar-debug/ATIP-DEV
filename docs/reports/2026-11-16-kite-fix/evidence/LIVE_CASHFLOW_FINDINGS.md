# LIVE book cash flows — findings (owner item 8), 16 Nov 2026

Read-only investigation of `atip_data/backups/atip-scheduled-20261115-191511.db` (opened `mode=ro&immutable=1`),
`atip_data/atip.log` and `launchd.out`. Nothing was written to any real database. All times IST.

## Verdict: (b) — it is still the PA-01 paper-cash bug, not withdrawals or transfers

The LIVE book (`pnl_daily` env LIVE, the real Dhan account) has stored the **paper simulator's balance as its cash on
every one of its 24 rows, 25 Sep – 13 Nov 2026**. The fix (PA-01, `portfolio/pnl.py _live_cash` → `available_balance(account=True)`)
was merged into `master` at **22:42 IST on 5 Nov** (`fd0e93a`), but the Mac's ATIP process was last started at
**21:10:59 IST on 5 Nov** (`atip.log` line 73604; no later "Starting ATIP" up to 00:17 IST on 16 Nov), so the running
code still asks the execution broker (the paper simulator) for the LIVE cash. The PA-01 history repair
(`tools/repair_live_pnl_daily.py`) has never been run.

No money left the real account through ATIP: the LIVE holdings never changed and no LIVE order was ever placed.

## The numbers

| Check | Result |
|---|---|
| LIVE cash = PAPER cash on the same date (to the paisa) | **24 of 24** rows with a cash figure (23 Oct has none: the Dhan call failed) |
| `paper_account.balance` on 13 Nov | ₹2,62,860.94 = LIVE cash on 13 Nov |
| LIVE holdings (`portfolio_holdings`, 8 symbols) | identical symbol / qty / avg price on every date 25 Sep – 13 Nov; cost ₹88,497.96 throughout |
| LIVE orders (`order_log.mode = 'LIVE'`) | **0** ever (96 rows, all PAPER: 92 PLACED, 3 BLOCKED_RISK_LIMIT, 1 FAILED) |
| Dhan fund-limit snapshots stored | **none** — `broker_health_check` records only "fund-limits call accepted"; the real balance is not kept anywhere in the DB. The 5 Nov audit read ₹2,350.16 (read-only `fundlimit`, ~20:20 IST) |
| Restart after the PA-01 merge | **none** (last start 5 Nov 21:10:59; merge 5 Nov 22:42:37) |

Every LIVE cash step is the paper sessions' cash step on the same day:

| Date | LIVE cash | Δ LIVE cash | Δ PAPER cash | Δ LIVE holdings value | stored LIVE day P&L | stored drawdown |
|---|---:|---:|---:|---:|---:|---:|
| 30 Oct | 5,04,275.50 | −589.74 | −589.74 | −305.00 | −894.74 | 0.91% |
| 2 Nov | 5,04,275.50 | 0.00 | 0.00 | +2,386.53 | +2,386.53 | 0.51% |
| 3 Nov | 5,04,275.50 | 0.00 | 0.00 | +206.03 | +206.03 | 0.47% |
| 4 Nov | 3,60,581.04 | −1,43,694.46 | −1,43,694.46 | +98.27 | −1,43,596.19 | 25.09% |
| 5 Nov | 3,17,449.33 | −43,131.71 | −43,131.71 | +418.31 | −42,713.40 | 32.41% |
| 6 Nov | 2,94,339.31 | −23,110.02 | −23,110.02 | +908.58 | −22,201.44 | 36.21% |
| 9 Nov | 3,11,088.94 | +16,749.63 | +16,749.63 | +413.32 | +17,162.95 | 33.27% |
| 11 Nov | 2,84,639.96 | −26,448.98 | −26,448.98 | +76.22 | −26,372.76 | 37.79% |
| 12 Nov | 2,62,543.30 | −22,096.66 | −22,096.66 | −676.44 | −22,773.10 | 41.70% |
| 13 Nov | 2,62,860.94 | +317.64 | +317.64 | −928.95 | −611.31 | 41.80% |

- 2 Nov → 13 Nov the LIVE "cash" fell **₹2,41,414.56** (₹5,04,275.50 → ₹2,62,860.94): exactly the paper sessions
  moving cash into paper positions (PAPER positions at cost ₹0 → ₹2,46,512.44, PAPER realised +₹7,723.01). Before
  30 Oct the paper book held nothing, so the paper balance (₹5,04,865.24) sat unchanged and the bug was invisible.
- **Real LIVE trading P&L, 2 → 13 Nov: +₹515.34** (the 8 holdings' unrealised P&L −₹12,365.79 → −₹11,850.45);
  since 25 Sep: −₹1,844.99. Not −₹2,41,414.
- The stored peak ₹5,83,357.74 (25 Sep) = paper cash ₹5,04,865.24 + LIVE holdings ₹78,492.50. The 41.8% drawdown is
  ₹3,39,508.45 against that peak: paper cash moved into paper shares, read as a LIVE loss.
- PAPER book is correct and separate: equity ₹5,09,048.24 on 13 Nov, peak ₹5,11,473.46, drawdown 0.47%; largest
  PAPER day loss −₹1,525.76 (13 Nov).

## Does `max_drawdown_pct` (10%) trip on this, or on withdrawals?

Configured (`atip_data/config.json`, read-only): `risk_limits` = max_order_value 10,000, max_orders_per_day 10,
max_open_positions 10, **max_daily_loss_value 10,000, max_drawdown_pct 10**; `broker_env` = PAPER.

- **Today, nothing is blocked by the 41.8%.** `orders/risk.py _loss_checks` measures the book the order goes to
  (`_pnl_env`): a PAPER (or SANDBOX) order is measured against the PAPER book (0.47%). The LIVE 41.8% shows on the
  dashboards (`/api/pnl`, Command, Workspace) only.
- **The day `broker_env` turns LIVE, every BUY would be refused** by max_drawdown_pct (41.8% > 10%; after the restart
  with real cash, ~86% against the stored ₹5.83 lakh peak — the PA-01 report's warning).
- **Yes, before this change a real withdrawal could trip both loss limits.** `risk_state` set equity (cash + holdings)
  against the previous `pnl_daily` row and the highest stored `peak_equity`; nothing told a withdrawal from a loss.
  Withdrawing ₹50,000 from a ₹5 lakh LIVE account read as a ₹50,000 day loss (> max_daily_loss_value ₹10,000) and a
  10% drawdown (= the cap; anything more trips it). PAPER has the same exposure only through `PaperBroker.reset()`.

## Root cause

1. PA-01 (fixed in code on 5 Nov, not running): `_live_cash` asked the execution broker; under PAPER that is the
   simulator.
2. Structural (new, owner item 8): ATIP had no record of external cash flows at all, so day P&L and drawdown were
   "change in equity", which counts deposits as profit and withdrawals / transfers as losses.

## What the owner must decide

1. **Restart** the Mac's ATIP (stop window) so PA-01 is live — on a clean tree, after a backup, per the restart
   checklist. Until then every 16:49 IST job keeps writing paper cash into the LIVE book.
2. **Repair the LIVE history before the first 16:49 job after the restart** (writes the live DB → owner's yes):
   `python -m ops backup --kind pre-release`, then `python -m tools.repair_live_pnl_daily --paper-match`
   (dry run), then `--paper-match --apply`. The tool's old default `--through 2026-11-05` would now miss the
   6, 9, 11, 12 and 13 Nov rows (and any row written later before the restart); `--paper-match` (added on this
   branch) selects exactly the LIVE rows whose cash equals the PAPER cash of the same day. Without the repair the
   first real-cash row (~₹2,350 + holdings) is compared with ₹3.39 lakh and reads as a ~₹2.6 lakh loss / ~86%
   drawdown; the new flow detection will list that step as a *pending* external flow, but it must **not** be
   confirmed as a withdrawal — it is the PA-01 basis change.
3. **Detected flows: confirm by hand (default) or automatically?** The new code records an unexplained LIVE cash
   change (holdings unchanged, no LIVE order) as a PENDING flow and keeps counting it as P&L until the owner confirms
   it (`config.json "cash_flows": {"auto_confirm_detected": false}`). Fail-safe: a manual intraday or F&O loss at
   Dhan outside ATIP also changes cash with holdings unchanged and must not be hidden as a "withdrawal". Set it to
   `true` only if every Dhan trade goes through ATIP.
4. **Record real deposits / withdrawals** when they happen (manual path, below). A recorded withdrawal never moves
   day P&L or drawdown.

## What changed in code (branch `claude/kite-formula-fix-2026-11-16`, not committed, not live)

No database was repaired; nothing below has run against `atip_data/atip.db`.

- **`portfolio/cash_flows.py` (new):** table `portfolio_cash_flows` (env, IST date, kind DEPOSIT / WITHDRAWAL /
  TRANSFER_IN / TRANSFER_OUT / ADJUSTMENT, signed amount, status CONFIRMED / PENDING / VOID, source, ref, note;
  never deleted, a wrong entry is voided). `record`, `set_status`, `listing`, `confirmed`, `net_flow`, `pending`,
  `scale_peak`, `adjusted_series`, `detect`, `record_detected`, `detect_history`, CLI
  `python -m portfolio.cash_flows add|list|confirm|void|detect`. Created lazily; classified by the `portfolio_` rule
  in `enterprise/privacy.py` and listed as an OWNER table in `enterprise/scoping.py`.
- **`portfolio/pnl.py`:** `_day_pnl(s, prev, flow)` subtracts the net confirmed flow since the previous row; new
  `flow_adjusted(conn, env, on, equity_now)` recomputes the series (day P&L net of flows, peak scaled by
  (equity before + flow) / equity before, drawdown) from stored equity + flows without rewriting rows; `risk_state`
  uses it when the book has any confirmed flow (else the exact old `MAX(peak_equity)` path — PAPER unchanged) and
  now also returns `external_flow`, `flow_adjusted`, `pending_flows`, `unexplained_cash`; `record_daily_pnl` stores
  a LIVE unexplained cash change as a PENDING flow (warning in the log) and writes flow-adjusted `day_pnl` /
  `peak_equity` / `drawdown_pct`.
- **`orders/risk.py`:** `_loss_checks` (max_daily_loss_value, max_drawdown_pct) reads the flow-adjusted
  `risk_state`; a breached loss check carries a `note` naming any pending / unexplained cash move, and
  `pretrade_check` appends it to the refusal message; `--status` prints the external flows.
  (`execution/risk_engine.py`, Command, Workspace and `/api/pnl` read the same `risk_state`.)
- **`tools/repair_live_pnl_daily.py`:** `--paper-match` picks the LIVE rows whose cash equals the PAPER cash of the
  same date. On the 15 Nov backup: the old default (`--through 2026-11-05`) selects 18 rows; `--paper-match`
  selects **23** (25 Sep – 13 Nov, every row with a cash figure).
- **API:** `GET /api/v2/cash-flows?env=&status=` (flows + each book's flow-adjusted series), `POST /api/v2/cash-flows`
  `{env, kind, amount, date, note}`, `POST /api/v2/cash-flows/{id}` `{status: CONFIRMED|VOID, note, kind}` (token,
  423 in the stop window, 403 on a viewer).

### Detection on the backup (report only: `detect_history`, nothing written)

With the default threshold (₹1,000) the detector flags **6** LIVE cash steps — 4 Nov −₹1,43,694.46, 5 Nov
−₹43,131.71, 6 Nov −₹23,110.02, 9 Nov +₹16,749.63, 11 Nov −₹26,448.98, 12 Nov −₹22,096.66 (net −₹2,41,732.20)
— as "withdrawal / deposit", because the holdings did not move and no LIVE order was placed. They are **not** real
flows: they are PA-01 paper cash. This is exactly why detected flows default to PENDING and must not be confirmed
blind; the right fix for these rows is the repair (`--paper-match`). (Had they been confirmed, the LIVE drawdown
would read 0.38% instead of 41.8% — right in effect, wrong in the record.)

### Tests

`tests/test_cashflow_tax_2026_11_16.py` (18 tests, all pass): a recorded withdrawal leaves max_daily_loss_value and
max_drawdown_pct at 0 while the unrecorded one blocks with a "cash moved −50,000 … record it" note; a real −₹60,000
loss on top still trips both (13.33% vs 10%); a real loss alone trips max_drawdown_pct (12%); a book without flows
is measured exactly as before; the 16:49 job stores an unexplained change PENDING, and once confirmed the re-run
day P&L is 0 and the peak scaled; auto-confirm counts a deposit at once; detection stands down for a LIVE order or
changed holdings; void / confirm keep the record; `--paper-match`; the CLI. Existing suites re-run green:
test_pnl_audit_2026_11_05, test_session_cycles, test_session_reports, test_risk_controls (98), test_ux_shell,
test_w38_platform_compliance, test_enterprise_owner_book, test_ux_audit_fix_2026_11_03 (160), and others importing
the touched modules (282 pass; the only failures are other agents' files: a MySQL reserved-word scan of
`data/bhavcopy.py`, `data/instruments.py`, `ops/preopen_health.py`, `research/session_ab_replay.py`,
`strategy/breakout_retest.py`, and `test_order_paths_are_untouched`, which needs a committed, clean tree).
