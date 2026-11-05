# ATIP profit-calculation audit — 5 Nov 2026

| | |
|---|---|
| Prompt | 5 Nov 2026, 8:05 PM IST |
| Audit run | 20:06 – 20:40 IST, 5 Nov 2026 |
| Build audited | `master` `fde1951` (the code live on the Mac) |
| Data | SQLite backup of the live `atip.db`, 20:07:58 IST (read-only; nothing in the live database was written by the audit) |
| Dhan | `GET /v2/trades` and `GET /v2/orders` at 20:10:30 IST, `GET /v2/fundlimit` at about 20:20 IST. **No order was placed, modified or cancelled.** |
| Live dashboard | `/api/v2/periods`, `/api/command/pnl`, `/api/sessions`, `/api/pnl`, `/api/pnl/live` at 20:12:19 IST; `/home` and `/accounting?tab=pnl` at 20:17 IST (GET only) |
| Fixes | branch `claude/pnl-audit-2026-11-05` (worktree `.claude/worktrees/pnl-audit-2026-11-05`). **Not merged, not live.** |

The prompt names `atip/orders_engine.py`, `atip/data/orders.py`, `atip/data/dhan.py` and `atip/dashboard/server.py`. Those paths do not
exist. The code that does that work is `orders/rules.py` (triggers, exit legs, fills), `orders/broker.py` + `orders/paper.py`
(execution, `order_log`, funds check), `data/dhan.py` (Dhan client, envelope unwrapping), `orders/sessions.py` (session books),
`accounting/cycles.py` / `periods.py` / `ledger.py` (what Home and Accounting show), `portfolio/paper_report.py` (`/command/pnl`),
`portfolio/pnl.py` / `live_pnl.py` (`/api/pnl`, `pnl_daily`) and `dashboard/ux_pages.py` / `command_more.py` / `server.py`. All of them were reviewed.

## 1. The answer

**Both figures are right, and every source agrees.** The figures below are the app's own.

| 5 Nov 2026 (IST) | Trades closed | Gross | Charges | **Net of charges** | Tax (est.) | After est. tax (Home) |
|---|---:|---:|---:|---:|---:|---:|
| **Today** | 5 (S1 ×2, S5, S9, S10) | ₹1,704.13 | ₹358.01 | **₹1,346.12** | ₹569.08 | ₹777.04 |
| **Since the pools started** (29 Oct, 23:09 IST) | 10 | ₹4,324.87 | ₹715.37 | **₹3,609.50** | ₹1,204.66 | ₹2,404.84 |

- The ten sessions' open trades are up ₹1,672.82 at the latest marks. Together the ten pools hold ₹2,55,282.32 against the ₹2,50,000 they started with (+₹5,282.32 = ₹3,609.50 booked + ₹1,672.82 open).
- An independent recompute from the raw fills agrees to the paisa, except S5: ₹0.01 apart. That is rounding. The app rounds each order's charges to the paisa, as a contract note does (₹29.57 + ₹42.89 = ₹72.46); the recompute rounds the unrounded sum (₹72.47).
- **Dhan's trade book for today is empty** (0 trades, 0 orders). All ten sessions trade on **paper** (`broker_env` PAPER), so "Realised (Dhan)" is ₹0.00 by design: no session trade reaches Dhan.
- In every source the ten sessions add up to that source's grand total: Home, Accounting, `/api/v2/periods`, `/api/command/pnl`, `/api/sessions`, `session_cycles`, the general ledger and the recompute.
- The cumulative figure runs without a gap: −₹589.74 (30 Oct) → +₹2,853.12 (4 Nov) → +₹1,346.12 (5 Nov) = ₹3,609.50.
- **Found:** one high-severity bug outside the ten sessions. **PA-01:** the real (LIVE) account's P&L carries the *paper* account's cash. There are also two latent session-P&L bugs (PA-02, PA-03) and one label (PA-04). All four are fixed on the branch with tests. Seven further items are for the owner to decide (§5).

## 2. Where the numbers come from

ATIP computes realised P&L four independent ways. All four were checked against each other and against the recompute.

| # | Computation | Basis | Feeds |
|---|---|---|---|
| A | `orders/sessions.py` `_trades` → `usage`, `ledger`, `journal` | order rules: each exit leg against its entry's fill price, net of the fills' recorded charges | session cards, `/api/sessions`, the session gate's `max_loss_value`, pool cash and sizing |
| B | `accounting/cycles.py` (`session_cycles`) → `periods.py`, `ledger.py` | A's trades stored as cycles: gross, itemised charges, net, per-cycle tax estimate, after tax | **Home** "Today / Since start", **Accounting**, `/api/v2/periods`, general ledger |
| C | `portfolio/paper_report.py` `_replay` | paper fills (`paper_order`), average cost per symbol, charges of both sides | `/command/pnl`, `/api/command/pnl` |
| D | `orders/paper.py` `paper_position.realized_pnl` → `portfolio/pnl.py`, `live_pnl.py` | the whole paper account, average cost, **gross** (no charges), every paper trade since 8 Sep | `/api/pnl`, `/api/pnl/live`, `/execution`, `pnl_daily.realised_cum` |

## 3. Step 1: code validation

| Check | Verdict | Evidence |
|---|---|---|
| **a. Long / short, closed vs open** | ✅ | `orders/sessions.py:503` `_trades`: `sign × (exit − entry) × qty`, sign −1 for a SELL entry. Only executed exit legs realise. Open quantity is `held_qty`, valued in `ledger` as unrealised. Pools are long-only (`_apply_pool_rules` refuses a SELL entry), and the paper broker refuses a sale of shares it does not hold. C skips such a sale (`paper_report.py:83`), so a short can never book a phantom. Tests: long, short, open. |
| **b. Partial exits** | ⚠ fixed (PA-03) | One entry per trade: every exit leg is measured against the entry's own fill price, and the entry's charges are shared per share sold. C uses the **weighted average** cost per symbol. The results are the same while a session holds one lot of a stock, which auto-pick guarantees. But `usage()` booked every part of a trade on the day of its *last* part, so a trade sold in two parts on two days counted the first part twice. Pools always sell whole, so no pool figure was wrong; it would affect non-pool sessions with T1/T2 brackets. |
| **c. Charges** | ✅ (label fixed: PA-04) | Every paper fill is charged through `backtest/costs.py` `nse_delivery`. The rates are statutory: STT 0.1% each side, NSE 0.00297%, SEBI ₹10/crore, stamp 0.015% on the buy, GST 18% on brokerage + exchange + SEBI, ₹0 Dhan delivery brokerage, plus DP ₹15.93 per sale (ATIP's setting; Dhan's published rate may be ₹14.75). All 28 session fills match an independent recompute to the paisa. Home and Accounting show gross, charges, tax (est.) and "Realised = after charges and estimated tax", clearly labelled. `/execution` showed D's **gross** whole-account figure as plain "realised ₹9,354"; it is now labelled. |
| **d. "Today" = IST day** | ✅ | No hardcoded dates in any P&L path. "Today" is `date.today()`, and every fill and leg timestamp comes from `datetime.now()`. Both use the process clock, which is pinned to `TZ=Asia/Kolkata` in `deploy/launchd/com.atip.platform.plist`, the `Dockerfile` (the view-only server) and `cloud-init`. The fills confirm it: the ten opening fills on 4 Nov are stamped 09:15:28–09:16:17. A sale at 23:59:59 and one at 00:00:01 fall on their own days (test). The only risk is a process started *without* that TZ on a non-IST host; a test now pins both deploy files. |
| **e. Session attribution** | ✅ | `session_rules.rule_id` is the primary key, so a rule belongs to one session. Exit legs inherit their parent's session (`session_of_rule`). All 28 session fills carry an `ATIP-S#` tag that matches their rule's session; there are 0 untagged fills since the pools started and 0 orders referenced by two rules. Relinking a rule moves it; it is never counted twice (test). |
| **f. Rejected / cancelled / partial** | ⚠ fixed (PA-02) + open (PA-07) | Only `EXECUTED` rules with a fill price count; CANCELLED and FAILED rules and REJECTED paper orders are excluded. But `execute_rule` recorded the quantity **asked for** when the broker reported a partial fill (`filledQty`). P&L then counted shares never bought, and the exit legs were sized to sell them, which the broker refuses with "cannot sell". **Today's data is clean:** every one of the 28 executed session rules (18 buys, 10 sales) equals its fill exactly. |
| **g. None / NaN** | ✅ flagged, no data affected | `execution_quantity or 0` (`sessions.py:518`) treats a missing quantity as "nothing sold", so the trade stays open and nothing is booked; it is never a fake ₹0 close (test). `brokerage or 0` (`sessions.py:486`, `paper_report.py:54`) would undercount charges if a fill ever had NULL charges, but the paper broker always writes the column (0 fills affected). `ledger` values an open trade with no price at its entry (`sessions.py:612`), so its unrealised shows ₹0 without saying so (PA-10). |
| **h. Rounding** | ✅ | Money is rounded to the paisa per fill (charges, as on a contract note) and per trade, never below. Sums are rounded once at the end. `paper_position.avg_price` keeps 4 decimals (D only, gross). The largest gap anywhere is ₹0.01 (S5, explained above). |
| **i. Duplicates** | ✅ | `paper_order.order_id` is a primary key (a second row is refused; test). `session_cycles` has `UNIQUE(session_id, rule_id)` and `sync` uses `INSERT OR IGNORE`; the ledger posts with `UNIQUE(txn_id, line)`. Syncing three times leaves one cycle and the same ledger total (test). `order_log` has 0 repeated order ids and is not used for P&L. No WebSocket order-update path exists; fills come back synchronously. An exit leg retries only after the broker **refused** it (no order id). In LIVE, a timeout after Dhan has accepted an order could resend it (PA-07). |

## 4. Step 2: reconciliation (read-only)

The full output, with the per-trade table and the integrity checks, is in `evidence/reconciliation.md` (JSON beside it). Reproduce it with `tests/verification/pnl_reconcile.py` (§8).

How to read the columns:
- **Realised (app)** is gross, from `/api/v2/periods`.
- **Charges** and **Net** are the recompute's.
- **Diff** is the largest gap between the recompute and *any* app source: gross, charges, net and after-tax, across `session_cycles`, the general ledger, `/api/v2/periods`, `/api/command/pnl` and `/api/sessions`.

### Today (5 Nov 2026, IST)

| Session | # trades | Realised (app) | Realised (recomputed) | Realised (Dhan) | Charges | Net | Diff | Status |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| S1 | 2 | ₹551.36 | ₹551.36 | ₹0.00 | ₹143.49 | ₹407.87 | ₹0.00 | OK |
| S2 | 0 | ₹0.00 | ₹0.00 | ₹0.00 | ₹0.00 | ₹0.00 | ₹0.00 | OK |
| S3 | 0 | ₹0.00 | ₹0.00 | ₹0.00 | ₹0.00 | ₹0.00 | ₹0.00 | OK |
| S4 | 0 | ₹0.00 | ₹0.00 | ₹0.00 | ₹0.00 | ₹0.00 | ₹0.00 | OK |
| S5 | 1 | ₹1,088.64 | ₹1,088.64 | ₹0.00 | ₹72.47 | ₹1,016.17 | ₹0.01 | OK (rounding: per-order charges) |
| S6 | 0 | ₹0.00 | ₹0.00 | ₹0.00 | ₹0.00 | ₹0.00 | ₹0.00 | OK |
| S7 | 0 | ₹0.00 | ₹0.00 | ₹0.00 | ₹0.00 | ₹0.00 | ₹0.00 | OK |
| S8 | 0 | ₹0.00 | ₹0.00 | ₹0.00 | ₹0.00 | ₹0.00 | ₹0.00 | OK |
| S9 | 1 | ₹637.52 | ₹637.52 | ₹0.00 | ₹70.29 | ₹567.23 | ₹0.00 | OK |
| S10 | 1 | −₹573.39 | −₹573.39 | ₹0.00 | ₹71.77 | −₹645.16 | ₹0.00 | OK |
| **GRAND TOTAL** | **5** | **₹1,704.13** | **₹1,704.13** | **₹0.00** | **₹358.02** | **₹1,346.11** | ₹0.01 | **OK (rounding)** |

The app's net today is ₹1,346.12 (charges ₹358.01). After the per-trade tax estimate (₹218.95 + ₹221.96 + ₹128.17 + ₹0), Home shows **₹777.04**: S1 ₹188.92, S5 ₹794.22, S9 ₹439.06, S10 −₹645.16.

Today's closed trades, IST:

| Time sold | Session | Stock | Qty | Bought | Sold | Exit | Net |
|---|---|---|---:|---:|---:|---|---:|
| 09:22 | S5 | ANGELONE | 81 | ₹307.80 | ₹321.24 | trailing stop | ₹1,016.18 |
| 09:25 | S9 | DIXON | 2 | ₹12,080.04 | ₹12,398.80 | trailing stop | ₹567.23 |
| 10:09 | S1 | PVRINOX | 16 | ₹1,538.57 | ₹1,605.70 | trailing stop | ₹1,002.33 |
| 10:34 | S10 | REDINGTON | 69 | ₹367.98 | ₹359.67 | stop | −₹645.16 |
| 12:06 | S1 | PVRINOX (re-entry, bought 10:27) | 16 | ₹1,584.69 | ₹1,552.02 | stop | −₹594.46 |

### All sessions, since each pool started

| Session | # trades | Realised (app) | Realised (recomputed) | Realised (Dhan) | Charges | Net | Diff | Status |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| S1 | 3 | ₹30.40 | ₹30.40 | ₹0.00 | ₹212.27 | −₹181.87 | ₹0.00 | OK |
| S2 | 1 | ₹662.34 | ₹662.34 | ₹0.00 | ₹71.81 | ₹590.53 | ₹0.00 | OK |
| S3 | 1 | ₹1,217.52 | ₹1,217.52 | ₹0.00 | ₹73.12 | ₹1,144.40 | ₹0.00 | OK |
| S4 | 0 | ₹0.00 | ₹0.00 | ₹0.00 | ₹0.00 | ₹0.00 | ₹0.00 | OK |
| S5 | 1 | ₹1,088.64 | ₹1,088.64 | ₹0.00 | ₹72.47 | ₹1,016.17 | ₹0.01 | OK (rounding) |
| S6 | 1 | ₹632.10 | ₹632.10 | ₹0.00 | ₹72.14 | ₹559.96 | ₹0.00 | OK |
| S7 | 0 | ₹0.00 | ₹0.00 | ₹0.00 | ₹0.00 | ₹0.00 | ₹0.00 | OK |
| S8 | 0 | ₹0.00 | ₹0.00 | ₹0.00 | ₹0.00 | ₹0.00 | ₹0.00 | OK |
| S9 | 1 | ₹637.52 | ₹637.52 | ₹0.00 | ₹70.29 | ₹567.23 | ₹0.00 | OK |
| S10 | 2 | ₹56.35 | ₹56.35 | ₹0.00 | ₹143.28 | −₹86.93 | ₹0.00 | OK |
| **GRAND TOTAL** | **10** | **₹4,324.87** | **₹4,324.87** | **₹0.00** | **₹715.38** | **₹3,609.49** | ₹0.01 | **OK (rounding)** |

After the tax estimate: ₹2,404.84. Per session: S1 −₹400.82, S2 ₹457.23, S3 ₹895.64, S5 ₹794.22, S6 ₹432.96, S9 ₹439.06, S10 −₹213.45.

### Step 4: dashboard = API = DB = sum of the ten

| Period | Home `/home` | Accounting | `/api/v2/periods` | `session_cycles` (DB) | General ledger | Σ S1–S10 |
|---|---:|---:|---:|---:|---:|---:|
| Today, after est. tax | ₹777.04 | ₹777.04 | ₹777.04 | ₹777.04 | ₹777.04 | ₹777.04 |
| Since start, after est. tax | ₹2,404.84 | ₹2,404.84 | ₹2,404.84 | ₹2,404.84 | ₹2,404.84 | ₹2,404.84 |

Net of charges: `/api/command/pnl` (pools) gives today ₹1,346.12 and since start ₹3,609.50. `/api/sessions` Σ `pool.booked` is ₹3,609.50, and `session_cycles` Σ `net_pnl` is ₹1,346.12 / ₹3,609.50. **No mismatch, so no trade to name.**

### Step 5: cumulative (all ten sessions, net of charges)

| Day (IST) | Realised that day | Previous cumulative | Cumulative | `/command/pnl` that day |
|---|---:|---:|---:|---:|
| 30 Oct | −₹589.74 (S1 PNBHOUSING stop) | ₹0.00 | −₹589.74 | −₹589.74 |
| 2–3 Nov | ₹0.00 (no exits) | −₹589.74 | −₹589.74 | ₹0.00 |
| 4 Nov | ₹2,853.12 (S3, S6, S10, S2) | −₹589.74 | ₹2,263.38 | ₹2,853.12 |
| 5 Nov | ₹1,346.12 | ₹2,263.38 | **₹3,609.50** | ₹1,346.12 |

There are no gaps or jumps. The whole paper account's stored `pnl_daily.realised_cum` (D: gross, including September's non-session trades) steps by exactly the recomputed gross every day: −₹520.96 on 30 Oct, +₹3,141.70 on 4 Nov and +₹1,704.13 on 5 Nov.

## 5. Findings

### Fixed on the branch (delta patches, root cause, test)

| ID | Severity | What was wrong | Root cause | Fix |
|---|---|---|---|---|
| **PA-01** | **High** (wrong now; blocks every BUY the day `broker_env` turns LIVE) | The **LIVE book** (the real Dhan account, `/api/pnl`, `pnl_daily` env LIVE) stored the **paper account's balance as its cash** on all 18 days since 25 Sep. Today that was ₹3,17,449.33, while the real account holds **₹2,350.16** (read-only `fundlimit`, about 20:20 IST). The paper sessions' buys of 4 Nov therefore read as a LIVE **day P&L of −₹1,43,596.19** and a **25% drawdown**, and 5 Nov as −₹42,713.40 and 32%. Under LIVE, the stored peak (₹5,83,357.74) would read as an ~87% drawdown, so `max_drawdown_pct` would refuse every BUY. | `portfolio/pnl.py` `_live_cash` asked `orders.broker.available_balance()`, which asks the broker that would *receive an order*. Under PAPER that is the simulator. Separately, when a row with equity followed a row without, day P&L set equity against the previous row's unrealised P&L alone. | `orders/broker.py` `get_fund_limits(account=True)` / `available_balance(account=True)` ask the real Dhan account; `_live_cash` uses it. The pre-trade funds check is unchanged. New `portfolio/pnl.py` `_day_pnl` compares equity with equity, else unrealised + realised with the same; used by `risk_state` and `record_daily_pnl`. **History:** `tools/repair_live_pnl_daily.py` (dry run by default) clears the paper cash from the 18 stored LIVE rows. Trial on a copy: 4 Nov −₹1,43,596.19 → **+₹98.27**, 5 Nov −₹42,713.40 → **+₹418.31** (the real holdings' move). It writes the live DB, so the owner decides when it runs (§7). |
| **PA-02** | Medium (latent: partial fills are off in config; Dhan's order response reports no fill) | A partial fill was booked at the quantity **asked for**, so the session P&L and pool cash counted shares never bought. The exit legs were also sized to sell them, which the broker refuses ("cannot sell"), leaving the shares that *did* fill without a stop. | `orders/rules.py` `execute_rule` wrote `execution_quantity = quantity` (requested) and ignored the broker's `filledQty`. | Use `filledQty` when the broker reports less than was asked (`rules.py:1027`). Legs, OCO settlement and the note follow it. |
| **PA-03** | Medium (latent: pools sell whole) | A trade sold in parts over two days booked **every** part on the day of the last one. `realised_today`, and so the session's `max_loss_value` gate, counted the first day's part again. | `orders/sessions.py` `usage` summed whole-trade P&L by the last exit's date. | `_trades` adds `realised_by_day` (each exit leg's P&L on its own fill day), and `usage` sums that day's parts (`sessions.py:526–570`). Whole exits give exactly the same figure as before. |
| **PA-04** | Low (label) | `/execution` said "realised ₹9,354" beside session pages that say realised = after charges and tax (₹2,404.84). It is the **gross**, whole-paper-account figure (D), including September's RELIANCE and CGPOWER trades. | No label. | Now reads "realised to date ₹9,354 (gross, before charges; whole paper account)". The `live_pnl.py` docstring says GROSS. |

### For the owner (not changed: each is a design choice or touches order behaviour)

| ID | Item | Recommendation |
|---|---|---|
| **PA-05** | Home's headline "realised" is **after a per-trade tax estimate with no same-day set-off**. Today S1 won ₹1,002.33 on PVRINOX and lost ₹594.46 re-buying it. The win is taxed ₹218.95 and the loss gets nothing, so S1 shows ₹188.92 against a net of ₹407.87. The total is ₹777.04 against a net of ₹1,346.12. This is labelled, and the year-end tax statement does the set-off. | Consider showing **net of charges** (₹1,346.12) as the headline, with tax (est.) on the line below. |
| **PA-06** | One page shows two "P&L" figures per session: the session card's **P&L** (equity − start, before tax, open trades included: S1 −₹181.87) and the table's **Since start realised** (closed only, after tax: S1 −₹400.82). | Label the card "P&L incl. open, before tax". |
| **PA-07** | **Before LIVE:** `execute_rule` marks a rule EXECUTED when the broker *accepts* the order. Dhan's `place_order` returns no fill price, so LIVE P&L would use the **trigger price**, not the fill. A paper LIMIT or SL that rests is also marked EXECUTED at its limit. September's CGPOWER rules (before the sessions) show the gap: rule ₹913.70 against fill ₹914.16. A timeout after Dhan accepts an exit could also be resent. | Before LIVE: settle each rule from the broker's trade book (`GET /v2/trades` / order status) by order id, keeping it PLACED until it fills. |
| **PA-08** | S7's pool cash is **−₹15.70**. GAIL was sized at the trigger price, then filled at the simulator's own quote a little higher: 137 × ₹182.38 + ₹29.64 = ₹25,015.70 from ₹25,000. The books record this correctly; the sizing price was stale. | Size from the same quote the fill uses, or keep a 0.1% cash buffer. |
| **PA-09** | DP charge ₹15.93 per sale is ATIP's setting; Dhan's published rate appears to be ₹12.50 + GST = ₹14.75. All ten sessions pay it on every sale (about ₹1.18 per sale too pessimistic if Dhan's rate holds). | Check one Dhan contract note and set `backtest.cost_overrides.dp_charge_per_sell`. |
| **PA-10** | `ledger()` values an open trade with no price at its entry, so its unrealised shows ₹0 without saying so. The `/command/pnl` report lists such stocks as "unpriced"; the session ledger does not. | Add an "unpriced" flag to the session's open rows. |
| **PA-11** | The tracker xlsx/csv and `PATCH_NOTES.md` are also edited by the unmerged branch `claude/verify-2026-11-05`. | Tracker rows PA-01..PA-11 are listed here and get added at merge time, so the two branches do not conflict in the binary xlsx. |

## 6. Tests

New: `tests/test_pnl_audit_2026_11_05.py`, 20 tests.

| Area | Tests |
|---|---|
| Long and short P&L | long trade net of both sides' charges; short trade won and lost; open quantity not realised |
| Partial exit | part sold books its share of the buy charges; parts on two days book on their own days (PA-03); fill-based report uses the average cost and skips a sale with nothing held |
| Charges | statutory delivery rates itemised to the paisa; cycle figures keep gross / charges / net / tax apart, and a loss carries no tax |
| IST day boundary | 23:59:59 and 00:00:01 sales on their own days; Home's Today counts cycles closed on the IST day; launchd and the Docker image run on Asia/Kolkata |
| Session attribution | every fill in exactly one session, legs inherit, totals add up (rules, fills, `/command/pnl`); relinking moves a rule, never doubles it |
| Duplicate de-dup | a second `paper_order` row with the same id is refused; syncing cycles three times leaves one cycle and the same ledger total |
| None handling | an executed leg with no quantity closes nothing and books nothing |
| PA-01 / PA-02 | LIVE cash is the real account even under PAPER; day P&L compares like with like; the repair clears the paper cash once (re-run is a no-op); a partial fill records and protects only what filled |
| Cross-check | `pnl_reconcile.py` run on a replayed day matches the app to ₹0.01 with every integrity check clean |

Against untouched `master` (`fde1951`), the 7 tests that cover PA-01/02/03 fail, as they should. Two of them fail on master only because they also read the new `realised_by_day` field. With the fixes, all 20 pass.

**Full suite**, run on both trees side by side from 20:23 to 20:33 IST, after the market closed (no order-monitor race):

| Tree | Passed | Failed | Skipped |
|---|---:|---:|---:|
| `master` `fde1951` + the new tests | 2,552 | 10: the 7 audit tests above, plus 3 `test_w39_perf_budget` (sandbox refuses local sockets) | 65 |
| this branch | **2,559** | **3**: the same 3 sandbox-socket tests, nothing else | 65 |

`test_ui_fixes_v2.py::test_order_paths_are_untouched` guards the order code. It allows only listed files to change and needs a clean tree. It now lists `orders/sessions.py` with the reason, as the 3 and 4 Nov reviews did for theirs. It passes once the branch is committed (checked on a temporary commit that was undone at once). Until the branch is committed it fails by design.

## 7. Deploying (owner's go-ahead needed for each step)

1. **Merge** `claude/pnl-audit-2026-11-05` into `master`. It merges cleanly with `claude/verify-2026-11-05`: that branch changes `settle_oco_group` in `orders/rules.py`, this one `execute_rule`. Re-add the tracker rows after whichever merges second.
2. **Back up, then repair the LIVE history:**
   1. `python -m ops backup --kind pre-release`
   2. `python -m tools.repair_live_pnl_daily` (dry run: the 18 rows and their new day P&L)
   3. `python -m tools.repair_live_pnl_daily --apply`
   
   Run this **before** the next 16:49 IST `pnl_daily` job after the restart. Otherwise the first row with the real cash (₹2,350) is compared with the paper-inflated row before it. The like-for-like basis (PA-01) prevents the false *profit* in that case, but not the false *loss* against a stored paper peak.
3. **Restart** the Mac's ATIP in the stop window (Python changes), on a clean tree (`git status`), after the backup.
4. **Rollback:** `master` at `fde1951`. The repair keeps the holdings figures, and the cash it clears was the paper balance, never the real account's.

## 8. Re-running the audit

```
python -c "import sqlite3; s=sqlite3.connect('file:atip_data/atip.db?mode=ro', uri=True); d=sqlite3.connect('snap.db'); s.backup(d)"
python -I tests/verification/pnl_reconcile.py --db snap.db --date YYYY-MM-DD --dhan dhan.json --api DIR --out OUT
```

- `--api DIR` holds the saved GET responses (`live_api_v2_periods.json`, `live_api_command_pnl_scope_pools_by_day.json`, `live_api_sessions.json`).
- `--dhan` is `{"get_trade_book": dhan.get_trade_book(), "get_order_list": dhan.get_order_list()}`, saved as JSON.
- The script opens the database read-only and imports nothing from `orders/`, `portfolio/` or `accounting/`.
