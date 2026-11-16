# ATIP testing report — 9–13 Nov review fixes + Kite breakout-retest port, 16 Nov 2026 (PAPER only)

Owner prompt: 15 Nov 2026, 11:52 PM IST. Work and tests: 16 Nov 2026 from 00:00 IST. **Every time here is IST.**

Build under test: branch `claude/kite-formula-fix-2026-11-16` (from master `7244fd0`), worktree
`.claude/worktrees/kite-fix-2026-11-16`. Commits and the merge are recorded in §9.

**Execution mode held throughout:** simulate only. No call to any broker method that places, modifies or cancels an
order, Forever Order, Super Order or GTT. `broker_env` stayed `PAPER`; the LIVE book's holdings were not touched. Every
broker-side construct is a local `DUMMY-` record (`algo_dummy_gtt`). Proven by §2's safety test and the replay's call log.

## 1. Environments

ATIP has no hosted Test or Prod server (tracker UXR-11; `docs/HOSTED_DEPLOYMENT.md` describes the view-only web copy,
which never trades). As in the 26 Oct – 6 Nov reports, "Test" and "Prod" are local instances of the branch's code in
scratch folders (no config.json, vault or broker token reaches them); the trading Mac (launchd `com.atip.platform`,
port 8000) is the production deployment (§9).

| Name | URL | Database | Settings | Purpose |
|---|---|---|---|---|
| Test | http://127.0.0.1:8861 | fresh (`main.py --init`), arms applied, no breadth / scan stored | `ATIP_ENV=test`, schedule off, order monitor off | empty and fail-closed states (gate CLOSED with no breadth) |
| Prod | http://127.0.0.1:8862 | SQLite backup of the live `atip.db` (00:30:27 IST 16 Nov), breadth from 1 Jan, arms, scan as of 13 Nov | `ATIP_ENV=staging`, schedule off, order monitor off | today's data, every changed page |
| Replay | — | a copy of the Prod snapshot | fake quote feed, broker clients replaced by recorders | Monday's first passes through the real code (§4) |

Launch entries `kgf-test`, `kgf-prod` in `.claude/launch.json`. Machine: macOS (Darwin 23.6.0), Python 3.15, the
project's `.venv`. Browser checks: the desktop app's built-in Chromium at 1440 px and 390 px (mobile emulation;
`?app=1` = the apps' user agent), light and dark.

## 2. Evidence reproduction (§6.1)

`.ux_build/kite-gap-2026-11-15/README.md` run order, against `atip_data/backups/atip-scheduled-20261115-191511.db`
opened read-only (`mode=ro&immutable=1`), 00:00:48–00:01:18 IST (copy of the scripts with writable paths:
`.ux_build/kite-fix-2026-11-16/repro/`). Then ported to `research/session_ab_replay.py` and `research/kite_retest_study.py`
(48 tests) and re-run 00:28–00:30 IST.

| Check | Expected | Reproduced |
|---|---|---|
| Week 9–13 Nov MTM net = `pnl_daily` PAPER equity change | −₹328.30 | −₹328.30 (₹5,09,376.54 → ₹5,09,048.24); 20 closed, 11/9, +₹2,298.90 |
| Since 29 Oct | 34 cycles, 19 wins, +₹4,800.55 | identical; avg win +3.037 %, avg loss −2.419 %, charges ₹70.63 |
| Calibration | 34/34 exit reasons, corr 0.97 | 34/34, corr 0.968 |
| Rank-fill population (daily, 47 days) | 446 trades, X1 31.6 %, −0.90 % | 446, 31.6 %, −0.895 %; 14/14 variants negative (−0.82 … −2.30 %) |
| 44 real entries | X1 3,064 … X9 14,147 | identical (X2 2,683, X6 6,679, X7 6,922, X8 11,957) |
| K1 | ~2,349 trades, +0.67 % | 2,349, 42.3 %, +0.67 %, PF 1.23; gated b50≥50 1,685 +0.97 % (last 12 m n 177 +1.14 %) |
| Setups at the 13 Nov close | 11 | the same 11, same buy / stop / tick (KALYANKJIL 669.00 / 635.55 …) |
| Breadth 13 Nov | b20 80.7, b50 56.8 | 80.69 / 56.79 (`scores/breadth.py`); 13/15/29 Oct b50 20.8 / 19.3 / 20.1 |

The production scanner keeps the Sunday 8 Nov Muhurat-session candle (the spec has no weekend filter; the study dropped
weekend rows), so breakouts before 8 Nov are one candle older and three volume multiples differ slightly (KALYANKJIL
5.75× vs 5.73×). With weekend rows dropped it reproduces the study's CSV exactly.

**Paper realism (KGF-17), calibration before / after:** 5 bps 34/34, corr 0.968, Σ sim ₹4,069.89; 15 bps on stop and trail
exits 34/34, corr 0.968, Σ sim ₹3,230.39 (stop-exit error median −5.9 bps; 10 bps fits best at −0.9 bps). BHEL 6 Nov
was not a gap: its 09:15 bar opened at 415.90 above the 410.78 stop; the 408.20 fill was inside one bar.

## 3. Automated tests

| Run | Time (IST) | Result |
|---|---|---|
| The three merged branches' own tests (verify 5 Nov, RCA, RCA-13, auto-pick, UI fixes v2) | 00:02:49–00:03:08 | **91 passed** |
| Full suite on the merged branch, before any KGF change (baseline) | 00:03–00:12:26 | 2,586 passed, 65 skipped, 6 failed (below) |
| New KGF tests, all six files together | 00:44:00–00:44:29 | **166 passed** (order path 24, scanner 47, replay ports 30 + 18, data / ops 29, tax / cash flows 18) |
| Full suite on the final tree (code commit `e1f9fe2`) | 00:41:42–00:51:20 | **2,754 passed, 65 skipped, 4 failed** — all 4 pre-existing |
| Session-board tests after the board fix (`2b8a3f4`) | 00:49 | 100 passed (+ the order-path check, which passes once committed) |

The 4 failures, each also failing on untouched master in this sandbox: `test_w39_perf_budget` × 3 (real local sockets,
refused by the sandbox) and `test_dhan_realtime_feeds::test_chain_tick_states_off_idle_and_live` (time-of-day; fails the
same on HEAD exported to a temp folder). The baseline's other two (`test_mysql_backend` reserved word in a merged
evidence script; `test_order_paths_are_untouched` with uncommitted files) were fixed / pass on the commit.
**Regressions introduced: 0.** Tests updated on purpose: `test_autopick._signals` and `test_score_audit._eligible` now
seed a fresh LTP (a pick needs one, KGF-02); `test_ui_fixes_v2` allows the KGF order-path files; `test_session_reports`
reads the new "tax reserve" label.

New test files: `tests/test_kite_formula_fix_2026_11_16.py` (tick cases; breadth; gate on / off / fail-closed; rank-fill
respects the gate and the control does not; no fresh LTP → `waiting_for_ltp`; LIMIT at min(LTP, close) × 1.005 →
892.40; skipped_falling logged once; 14:45 cut-off; `confirm_live` missing / false / "true" / 1 → rejected; dummy
fields and the Kite message; repeated, 20 concurrent threads and a module reload → one record; each blocked case
(order, GTT, position, holding, paper position); arm exits, ATR and risk-budget sizing; retest arm: DUMMY buy → no stop
before the fill → fill ≤ buy → STOP + TARGET → DUMMY OCO; time exit on the Nth trading day; the safety test),
`tests/test_breakout_retest.py` (one golden test per accept / reject rule, normalisation, API, page, the 13 Nov
reference), `tests/test_session_ab_replay.py` and `tests/test_kite_retest_study.py` (engine parity with the 15 Nov
pack, charges, the backup reproduction), `tests/test_instruments_preopen_bhav.py`, `tests/test_cashflow_tax_2026_11_16.py`.

**Safety (§6.3):** `test_no_order_changing_broker_method_is_ever_called` gives the dummy-GTT paths Dhan and Kite client
doubles where every method exists, runs them with `confirm_live` true, false and missing and through the error paths
(bad price, no LTP), runs `sync`, `maintain` and a full retest auto-pick pass, and asserts that no `place_*`,
`modify_*`, `cancel_*`, `*_gtt`, `exit_all_positions`, `kill_switch` or `convert_position` was reached; `ReadOnlyBroker`
raises on each of them. The Monday replay on the live-DB copy (§4) logged only `get_order_list`, `get_positions`,
`get_holdings`, `get_forever`.

## 4. Replay of Monday's first passes on the live-DB copy (Prod data, 00:31 IST)

`.ux_build/kite-fix-2026-11-16/replay/replay_monday.py` against a copy of the Prod snapshot: arms applied, breadth
computed (13 Nov: b20 80.69 %, b50 56.79 %, gates OPEN), the 13 Nov scan stored. Quotes from a fake feed at the 13 Nov
closes; every broker client a recorder. Schedule off (the 21:00–06:00 stop window otherwise holds everything — the first
attempt showed `HELD_HALTED` and "trading is halted" for all ten, as it should).

| Step | Result |
|---|---|
| 09:16, three stops (S3 AUROPHARMA, S5 KOTAKBANK, S6 KALYANKJIL) | 3 × PLACED (exit legs, PAPER) |
| 09:20 auto-pick | S3 (retest K1) → **KALYANKJIL** retest of the 12 Nov breakout: `DUMMY-A6D2…` resting buy **669.00**, stop **635.55** planned after the buy, ~18 sh (₹625 ÷ 5 % budget); S5 (control) → IGIL RANK-FILL, LIMIT **441.05** on a fresh LTP 438.90 (= close × 1.005 = 441.09, tick 0.05 down), 54 sh (whole cash); S6 (ATR) → GRASIM, ATR stop **4.84 %**, LIMIT 3,049.10, 4 sh (risk-capped); the other seven still hold their 13 Nov positions |
| 10:05, prices at the triggers | KALYANKJIL filled **668.00** (≤ 669, LIMIT); IGIL 440.40 (≤ 441.05); GRASIM 3,044.60 (≤ 3,049.10) |
| Legs after the fills | KALYANKJIL STOP 634.60 / TARGET 734.80, max 20 days; IGIL STOP 431.55 trailing 1 % from 453.61, TARGET 453.65 after 15 days; GRASIM STOP 2,897.10 trailing 2.42 % from 3,192.09 |
| DUMMY records | BUY → TRIGGERED at 668.00; OCO `DUMMY-8395…` ACTIVE, stop 634.60, target 734.80 |
| Off-tick prices on new rules | **none** |
| Broker calls | read-only only (`get_forever`, `get_holdings`, `get_order_list`, `get_positions`); **0 order-changing** |

Dry run (`python -m orders.autopick --dry-run`, Prod snapshot): breadth line, pre-open gate OK (before 08:45), each arm
with its gate state and rules; all ten sessions WAITING ("occurrence #n holds …": every session still holds its 13 Nov
position, so on Monday a session picks only after its trade exits).

## 5. Pages: Test and Prod, dark and light, 1440 px and 390 px

Playwright sweep (`.ux_build/kite-fix-2026-11-16/ui/sweep.js`, from the 4 Nov verification sweep): every page × theme ×
width — HTTP status, JS / console / server errors, the applied theme, horizontal page scroll, axe-core (WCAG 2 A / AA,
2.1 AA), one screenshot each in `docs/testing/screenshots/kgf-2026-11-16/` (`<server>_<page>_<theme>_<width>.png`).
Pages: Home, Sessions, a session (S3), **Arms (A/B)** `/ab` (new), **Algo Signals** `/algo-signals` (new), Accounting
› Tax (FY estimate, new), Accounting, Post-market RCA, More, Reports, Command Center.

| Server | Runs | Result |
|---|---|---|
| Prod (live-DB snapshot) 00:41:59–00:44:00 | 44 | **44 clean** (HTTP 200, no JS or server error, no horizontal scroll, 0 axe violations) |
| Test (fresh DB) 00:45:31–00:47:14 | 44 | **44 clean** |

88 screenshots. Per tab (both servers, both themes, both widths): Home ✔, Sessions ✔, Session S3 ✔, Arms (A/B) ✔,
Algo Signals ✔, Accounting › Tax ✔, Accounting ✔, Post-market RCA ✔, More ✔, Reports ✔, Command Center ✔.

What each new or changed page showed:
- **Prod `/ab`:** gate banner "breadth20 80.7 % ≥ 50 % → OPEN for S10 · breadth50 56.8 % ≥ 50 % → OPEN for S1–S4, S6–S9 ·
  no gate: S5 · exits always run · Strategy today: rank-fill on the ATIP score and the Kite retest, not AI"; the ten arms
  and their rules; the weekly A/B section (first report Sun 22 Nov 18:00 IST); DUMMY records (none on the snapshot).
- **Test `/ab`:** fail-closed: "Regime gate: no breadth stored yet, so every gated arm waits".
- **Prod `/algo-signals`:** 11 triggered breakouts of 495 scanned (499 scored, as of 13 Nov), ranked by age then
  distance; live ticks "Closed · 16:45" (prices from the last close); 390 px: the table scrolls inside its card.
  **Test:** "No breakout retest setups match the current parameters."
- **Home and Sessions:** the same gate banner; Sessions' tile reads "Tax reserve (per-cycle) ₹3,065.29 — cash set
  aside, not tax due". **Accounting › Tax:** "FY 2026-27 · FY STCG tax estimate (after set-off) · paper" with the CA line.

## 6. Mobile apps

2026.11.16.1 — Android `versionCode` 10, iOS `CFBundleVersion` 10. Native change: the banner shows the regime gate
(`/api/v2/shell/state` → `regime_gate`) as an amber strip while it holds the gated arms; everything else comes from the
server's screens (Sessions, More → Sessions → Arms (A/B), More → Reports → Algo Signals, the tax lines).

| Build | Result |
|---|---|
| iOS | **Not built**: this Mac has only the Xcode command-line tools (`xcodebuild` needs Xcode, which needs the owner's Apple ID). `swiftc -parse` on every Swift source: exit 0 (00:36 IST) |
| Android | **Debug APK built 06:04 IST 16 Nov** after the toolchain install (Temurin 17 + Android SDK): `mobile/android/app/build/outputs/apk/debug/app-debug.apk`, `com.atip.mobile` 2026.11.16.1 / versionCode 10, 5,627,887 bytes, sha256 `45bc49193da49796…`. Not a signed release bundle (no signing key on this Mac). Until then this row read "Not built: no Java runtime, Android SDK or Gradle on this Mac" (corrected 16 Nov by the audit) |

To build: Xcode → `xcodegen` in `mobile/ios`, Archive; Android Studio → `mobile/android`, Build → Generate Signed Bundle.

## 7. Owner items still open (not done on purpose)

| Item | Why it waits | What to run |
|---|---|---|
| KGF-08 LIVE `pnl_daily` history repair | It writes the live DB; item 8 says "report what you find before changing anything". Under PAPER the 41.8 % blocks nothing; after this deploy the LIVE book reads the real account (PA-01), so the stored LIVE peak makes LIVE drawdown read ~86 % until repaired (LIVE BUYs only; broker_env is PAPER) | `.venv/bin/python -m tools.repair_live_pnl_daily --paper-match` (dry run), then `--apply`, after a backup, before 16:49 IST |
| `cash_flows.auto_confirm_detected` | Detected LIVE cash moves stay PENDING (still P&L) until confirmed: a manual loss must not be hidden as a withdrawal | `python -m portfolio.cash_flows add|confirm` for real deposits / withdrawals |
| PA-05 Home headline | Still net minus the per-cycle reserve (now labelled "tax reserve"); switching to the FY estimate is the owner's call | — |
| Promote / retire an arm | Only after 30 closed trades per arm or 6 weeks, whichever is later: earliest **Sun 3 Jan 2027 18:00 IST**; weekly reports from **Sun 22 Nov 2026 18:00 IST** | `/ab`, `python -m portfolio.ab_report` |
| News AI | Off until `news_impact_eval` shows a lift as a filter on the arms | — |
| LIVE switch | Static-IP VPS, algo ID, MPP / LIMIT exits (KGF-20) | — |

## 8. Merge (CI → staging → prod)

| Step | Time (IST) | Result |
|---|---|---|
| CI: full suite on the branch | 00:41:42–00:51:20 | 2,754 passed / 65 skipped / 4 pre-existing failures |
| Staging: Test + Prod local servers, replay, sweep | 00:30–00:47:14 | replay passed; 88 / 88 page runs clean |
| Rollback branch `claude/live-running-2026-11-16` = `424ae48` (the code the Mac ran since 5 Nov 21:10:58) | 00:47 | created |
| Pre-release backup `atip_data/backups/atip-pre-release-20261116-004836.db` (verified, sha256 `d7d53fca…`, 748,449,792 bytes) | 00:48:36–00:48:45 | OK |
| Merge into master: `2e407e6` (`--no-ff`; master was `7244fd0`) | 00:51:59 | OK. 16 untracked screenshots in `docs/testing/screenshots/full-review-2026-11-03/` blocked the merge: byte-identical to the copies the 5 Nov verification branch committed, so they were moved aside (`.ux_build/kite-fix-2026-11-16/merge_moved/`), the merge recreated them, and `cmp` confirmed all 16 identical |
| Not pushed | — | `origin/master` unchanged (as in every earlier session) |

## 9. Production deploy on the Mac (launchd `com.atip.platform`, :8000)

| Step | Time (IST) | Result |
|---|---|---|
| `git status` in `/Users/agtci/ATIP` before the restart | 00:52 | no tracked changes (125 untracked entries, untouched: `.ux_build/kite-fix-2026-11-16/untracked_before_merge.txt`) |
| `launchctl kickstart -k gui/$UID/com.atip.platform` | 00:52:23 | pid 55822 (since 5 Nov 21:10:58, `424ae48`) → **14211**, `2e407e6`; log: "order_rules migrated — added: signal_close, pick_ltp, pick_ltp_ts, max_hold_days, algo_ref", "Order-rules monitor started"; new jobs registered (instrument_master 08:00, preopen_health 08:45 + retries, intraday_completeness 16:15, breadth_and_scan 21:20 + 07:30, ab_weekly_report Sun 18:00); stop window holds everything until 06:00 |
| `python -m scores.breadth --from 2026-01-01` (live DB) | 00:52:58 | 13 Nov: breadth20 80.7 %, breadth50 56.8 % |
| `python -m orders.arms --apply-defaults` (live DB, PAPER) | 00:52:59 | S1–S10 applied, each logged as an ARM session event; gates OPEN; open positions keep their legs |
| `python -m data.instruments --refresh` | 00:53:04 | Dhan scrip master (published 14 Nov 02:50 GMT) stored, fresh; ZOMATO→5097, TATAMOTORS→3456, MOFSL→14947, REITs resolve with no warning |
| `python -m strategy.breakout_retest --store` | 00:53:11 | 11 setups as of 13 Nov (KALYANKJIL 669.00 / 635.55 first) |
| `python -m orders.autopick --dry-run` | 00:53 | gates OPEN, pre-open gate "before check"; all ten WAITING — each still holds its 13 Nov position |
| `tools/backfill_intraday_bars.py --from 2026-10-06 --to 2026-10-23 --apply` (read-only Dhan data API) | 00:53:27–00:56:05 | 168,194 bars, 527 symbols; remaining gap 13 symbol-sessions (ICICIPRULI: Dhan returned none) |
| Live sweep, :8000 (Home, Sessions, Arms, Algo Signals, Accounting › Tax; dark / light; 1440 / 390) | 00:53:33–00:55:17 | **20 / 20 clean**; screenshots `live_*` |

**Not changed on the Mac (owner):** `risk_limits.max_orders_per_day` stays **10**. The owner chose 20 on 5 Nov (VF-04,
"to do at deploy" in the 5 Nov verification report), but editing the live `config.json` was refused by this session's
auto-mode permission check because this prompt did not ask for it. To apply it, copy `atip_data/config.json`, then set
`"max_orders_per_day": 20` under `risk_limits`; it is read on each order, no restart needed. The LIVE `pnl_daily` repair (§7) is also left.

**First live session:** Mon 16 Nov 2026 — 06:00 resume, 07:30 breadth + scan, 08:00 instrument master, 08:45 pre-open
gate, 09:15 order monitor and auto-pick. A session picks under its arm as soon as its 13 Nov trade exits.
