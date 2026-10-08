# W39b deployment runbook (owner, macOS)

**Release:** `ATIP-W39B` = the `master` commit that merges PR #5. After `git pull`, `git log -1 --format='%h %s'` shows it: a merge of `claude/wizardly-curie-fbjeoa`, or a commit titled "W39b: ...".
**Previous production:** `1d089a7` (W39, PR #4). This is the rollback target.
**Machine:** the Mac running the `com.atip.platform` LaunchAgent. On the old Windows machine, use `deploy\deploy_release.ps1`; the steps are the same.

The release changes nothing about trading safety: LIVE stays off, and every new automatic behaviour ships switched off (see [What ships switched off](#what-ships-switched-off)).

## 1. Before you start (2 minutes)

- Pick a time outside market hours (after 16:45 IST or before 07:00). The deploy restarts ATIP for about a minute.
- In the project folder, `git status` should show no modified tracked files. Your spreadsheets are untracked, and the preflight ignores them.
- The Mac is on IST, and the virtualenv `.venv` exists (README, Quick Setup).

## 2. Deploy (5 minutes)

```bash
cd /Users/agtci/Documents/Project_Documents/Projects/ATIP-dev

# get the release
git fetch origin
git checkout master
git pull --ff-only origin master
git log -1 --format='%h %s'                         # the PR #5 merge (see Release above)
git tag -a ATIP-W39B HEAD -m "W39b: tracker reconciliation and remaining features (PR #5)"
git branch -f backup/pre-w39b-master 1d089a7          # the rollback target

# dependencies (adds only what is missing)
.venv/bin/pip install -r requirements.txt

# dry run: prints the plan and runs the read-only preflight
deploy/deploy_release.sh ATIP-W39B

# deploy
deploy/deploy_release.sh ATIP-W39B --authorize
```

**What `--authorize` does:**
1. Takes a verified pre-release backup.
2. Unloads the LaunchAgent, so `KeepAlive` cannot restart the old code mid-migration.
3. Records the release manifest and copies `config.json` / `.env`.
4. Applies the additive migrations.
5. Loads the LaunchAgent again.
6. Waits up to 3 minutes for `/health/ready`, then checks every health endpoint and that `LIVE_TRADING_ENABLED` is false.
7. Writes `DEPLOYED` (or `FAILED`) to `atip_data/releases/history.jsonl`.

**It is done when the last line reads `DEPLOYED ATIP-W39B (...)`.**

## 3. Check it (5 minutes)

```bash
launchctl list | grep com.atip                 # 0 in the second column
tail -n 50 atip_data/launchd.err               # no Traceback
tail -n 3 atip_data/releases/history.jsonl     # DEPLOYED ATIP-W39B
curl -s http://127.0.0.1:8000/health/ready     # {"status":"READY",...}
```

Then open http://127.0.0.1:8000:

| Page | What to look for |
|---|---|
| `/research` → any stock | The scorecard, then the new **DVM view** card (three 0–100 bars and a zone), then the **Earnings surprise** card. "Insufficient history" is expected until the NSE filings backfill has run. |
| `/screener` | 45 presets, including "75-minute and daily both bullish", "DVM strong performers", "Positive earnings surprise" and "Chart pattern breakdowns". The new fields stay empty until the 20:30 / 20:35 / 20:50 jobs have run once. |
| Stock chart (any stock panel) | Diamonds for pattern breakouts, dots for signals and candle patterns, dashed pattern lines. The **Patterns** button hides them. |
| `/data-platform` → Multi-asset | Search a mutual fund and click it: returns, rolling returns, risk, category rank, SIP calculator. |
| `/wealth` | Unchanged figures; the Simple / Detailed toggle. |
| `/market-pulse` | The 20-level card shows OFI only when `depth20.enabled` is on and the Dhan Data API is subscribed. |

**One-time, after this deploy: rotate the dashboard write token.** An earlier hosted snapshot carried it. The fix is in this release, but the old token must be replaced:

```bash
rm atip_data/dashboard_token.txt
launchctl kickstart -k gui/$UID/com.atip.platform    # restart; ATIP writes a new token
```

Update any bookmark or script that sends the old token.

## 4. If something is wrong: roll back

```bash
deploy/rollback_release.sh backup/pre-w39b-master              # dry run
deploy/rollback_release.sh backup/pre-w39b-master --authorize  # code-only rollback
```

**The code-only rollback is normally enough.** Every W39b schema change is additive, so the W39 code runs on the newer database.

Restore the database only if the release damaged data. Add `--restore-backup <backup_id>`, using the `atip-pre-release-...` id the deploy printed in step 3, or the one from `python -m ops backups`. Data written after that backup is lost.

To restore the configuration copy as well, add `--restore-config-from ATIP-W39B`.

## What ships switched off

Each item is one setting, and each stays off until you turn it on.

| Setting (`atip_data/config.json`) | Default | What it does when you turn it on |
|---|---|---|
| `aggressive_enabled` | `false` | The aggressive strategy (`python -m strategy.live`) may open new positions. When off, it opens none, but it still manages open ones. It is not scheduled either way. |
| `cri_exit_threshold` | `null` | When a held stock's crash-risk index reaches this value (1–100), the aggressive strategy closes it with `MARKET_RISK_EXIT`. Look at the CRI history before you pick a value (for example 80). |
| `depth20.enabled` | `false` | Turns on the Dhan 20-level depth feed, its imbalance, and OFI (`ofi_levels`, `ofi_decay`). Needs the Dhan Data API. |
| `billing.provider` | `sandbox` | `razorpay` uses the Razorpay provider. It needs the vault keys (docs/BILLING_RAZORPAY.md), and only matters with the enterprise layer on. |
| `billing.razorpay.allow_live` | `false` | Allows `rzp_live_` keys, and only in the production environment. Test keys work without it. |
| `risk_limits.enforce_circuit_limits` | **`true`** | The only one that ships **on**. It refuses an order priced outside today's NSE circuit band, which the exchange would reject anyway. Set it to `false` to turn it off. |
| `execution.mode` / `live_trading_enabled` | `PAPER` / `false` | Unchanged by this release. Live orders still need both settings and the production environment, and the deploy checks this. |

**Per-report option, not a setting:** the wealth performance report's `slippage_model` (`fixed` by default; `impact` uses the square-root market-impact model).

**Scheduled jobs added (market days):**
- **20:35:** earnings surprise.
- **20:30:** the existing signal run now also computes the 75-minute rating.
- **20:50:** the existing scorecard job now also stores the DVM.
- The paper SIP job runs as before (W39b round 1).

Each job only reads data ATIP already stores.

## What still needs you (blockers)

| What | Who / what is needed | Until then |
|---|---|---|
| Running this deploy | You, on the Mac (section 2) | The code is on `master`; production runs the previous release |
| Live trading (EX-19 GTT, options one-click, the aggressive strategy) | Your explicit go-ahead, plus the Dhan forever-order API on the account | PAPER only |
| BR-08 sandbox check | A Dhan-issued sandbox token, then `python -m orders.sandbox_check --place-test-order` | Not run |
| Dhan Data API | The subscription | No 20-level depth / OFI, no 15-minute bars (so no 75-minute rating), no order-book pressure |
| ENT-04 Razorpay | A KYC-activated business account with Invoices and Subscriptions; keys in the vault | Billing uses the sandbox provider |
| ENT-07 / ENT-08 exposure | How ATIP is reached from the internet (tunnel / VPN / reverse proxy), the domain and TLS | Local only; Razorpay settles by polling, not webhooks |
| ENT-14 regulatory | Sign-off by a qualified professional (SEBI RA / IA; payments tax) | Reports carry disclosures; nothing is published |
| ENT-16 HA | Move the runtime to PostgreSQL and choose a host | SQLite on the Mac; the container image is ready |
| Consensus estimates | A licensed estimates feed | PEAD and research use ATIP's own history |
| SE-05 AI strategies | Evidence: a model that passes validation after more history (DP-23 backfill) | No ML strategy is activated |
| UAT-001 | You run the UAT journeys and record acceptance | Independent QA pending |

## How the scripts were verified

**`deploy/deploy_release.sh` and `deploy/rollback_release.sh` were run on Linux in a scratch clone:**
- a dry run;
- an authorized deploy that replaced a running instance (stop, backup, migrate, start, postcheck, `DEPLOYED`);
- a code-only rollback;
- a rollback with a database restore.

All postchecks passed.

**The LaunchAgent branch (`launchctl bootout` / `bootstrap`) runs only on macOS, so it was not exercised.** If it misbehaves, the manual equivalent is:

```bash
launchctl bootout gui/$UID/com.atip.platform
.venv/bin/python -m ops backup --kind pre-release
.venv/bin/python -m ops migrate
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.atip.platform.plist
.venv/bin/python -m ops release postcheck --wait 180
```

**The container image** (`Dockerfile`) is built and smoke-tested by `.github/workflows/docker.yml` on every PR (docs/CONTAINER_DEPLOYMENT.md). It is not needed for the Mac deploy.
