# One server, three apps: keeping GoKesari, bkesari (with ATIP) and agtci apart — plan, 23 Nov 2026

**Status:** draft for the owner. Step 0 is done (read-only) and the step 2 files are built and tested. No change has been made on the server yet. All times IST.

**Owner's request** (about 09:45 IST, 23 Nov): "check that when I say deploy and restart, it will only restart the ATIP or it will restart the other applications as well … create a draft to keep all these sessions separately, that B Kesari session will be separate as well as Go Kesari session, its deployment will be separate, not conflicting with each other. guide me the detailed steps to implement and start implementing it."

**Evidence:** the server inventory from 09:52:14 IST, 23 Nov (read-only, run by the VPS session) is in `.ux_build/session-review-2026-11-20/vps_inventory_2026-11-23.txt`.

---

## 1. Answer: "deploy and restart" restarts ATIP only

**Yes: only ATIP restarts.** Today's deploy is the live test:

- **The 09:36 restart touched only ATIP.** The systemd log for 09:35–09:38 IST shows only `bkesari-atip.service` stopping and starting at 09:36:13. Everything else in that window was GoKesari's and agtci's normal every-minute timers (gk-cron, gk-deploy, gk-db-health, agtci-health).
- **Every other service kept its earlier start time:**

| Service | Running since (IST) |
|---|---|
| caddy (the web server for all sites) | 17 Nov 07:17:49 |
| site@gokesari-prod-blue (gokesari.com) | 23 Nov 00:58:43 |
| site@gokesari-staging-green (test.gokesari.com) | 23 Nov 09:08:44 |
| bkesari-platform (bkesari.com) | 19 Nov 10:15:23 |
| agtci-api@prod (agtci.com) | 19 Nov 19:10:47 |
| PostgreSQL main 5432 / staging 5433 (GoKesari) | 16 Nov 22:18 |
| PostgreSQL bkesari 5434 | 17 Nov 15:01:03 |
| PostgreSQL agtci 5435 | 19 Nov 10:19:45 |
| **bkesari-atip (ATIP)** | **23 Nov 09:36:13, the deploy** |

- **Nothing is linked to ATIP's service.** Nothing depends on it: its only reverse dependencies are `multi-user.target` and `graphical.target`. It also has no `BindsTo` or `PartOf`, so restarting it cannot take another service down with it.
- **The deploy tool can only restart ATIP.** `deploy/vps_deploy.py` starts, stops and restarts only `bkesari-atip.service`, plus the `bkesari-atip-pgdump.service` backup (`DEFAULTS`, `Server.systemctl`).
- **ATIP's own service is locked down.** It runs as `bkesari`, can write only `/var/www/bkesari/atip` (`ProtectSystem=strict`), and has `NoNewPrivileges`.

**Nothing on the server stops a mistake from hitting other apps.** The one-unit restart comes from how ATIP's deploy is written, not from what its login is allowed to do:

| Risk | What it means today |
|---|---|
| **One root login for everyone** | `gkadmin ALL=(ALL) NOPASSWD:ALL`, and it is the only login (1 key). Every GoKesari session and the bkesari VPS session use it, so one wrong command can stop any app. |
| **The PostgreSQL umbrella** | `systemctl restart postgresql` restarts **all four databases at once**: GoKesari's live and staging, bkesari/ATIP and agtci. Only `postgresql@18-<name>` touches one. |
| **One Caddy for all sites** | Every deploy that reloads Caddy (GoKesari's `gk-deploy` does, on each release) reads every `/etc/caddy/sites/*.caddy`. A broken bkesari or agtci file would make GoKesari's release fail its Caddy reload. `systemctl restart caddy` drops every site for a moment; GoKesari's `reload_caddy` falls back to a restart if Caddy isn't running. |
| **OS updates** | `php8.3-fpm` (the shared PHP service; bkesari's PHP apps run in it) restarted at 06:39:11 IST on Sun 22 Nov, and no session asked for it. *Hypothesis:* automatic security updates. To be checked in step 5. |
| **CPU and memory** | 4 CPUs, 15 GiB RAM (11 GiB free, load 0.04 at 09:52). ATIP has no CPU weight and no memory floor. GoKesari builds are already capped (4 GB, 150 % CPU, nice 10), so this is low risk today. |

## 2. What is already separate

| | GoKesari | bkesari (+ ATIP) | agtci |
|---|---|---|---|
| Linux user | `gk-<site>` per site | `bkesari` | `agtci` |
| Code | `/srv/sites/<site>` | `/var/www/bkesari/*` | `/var/www/agtci/{test,prod}` |
| Services | `site@gokesari-*`, `gk-cron@*`, `gk-deploy` | `bkesari-atip`, `bkesari-platform` (+ a pool in the shared php8.3-fpm) | `agtci-api@{test,prod}` |
| Database cluster | `18/main` 5432, `18/staging` 5433 | `18/bkesari` 5434 | `18/agtci` 5435 |
| Caddy file | `gokesari-prod.caddy`, `gokesari-staging.caddy` | `bkesari.caddy` | `agtci.caddy`, `agtci.prod.caddy` |
| How it deploys | GitHub → staging queue → `gk-deploy` agent (one global lock, blue/green) | `vps_deploy.py` or by hand, as `gkadmin` | by hand, as `gkadmin` |
| Server login | `gkadmin` (root) | `gkadmin` (root) | `gkadmin` (root) |

**Shared by all three:** the `gkadmin` login, Caddy, the `postgresql` umbrella service and packages, php8.3-fpm and MariaDB (host services, per the VPS session's inventory note), WireGuard (`wg-quick@wg0`), the firewall, sshd, OS updates and reboots, and CPU, memory and disk.

## 3. Target

1. **One login per app, limited to that app.**
   - `bkdeploy` can do anything as `bkesari` and run `bk-ctl` as root. `bk-ctl` only starts, stops or restarts bkesari units and reads their logs.
   - `agtcideploy` follows the same pattern later. GoKesari decides its own `gkops` login.
   - `gkadmin` stays the server-admin login, for shared changes only.
2. **One session (or group of sessions) per app,** each with a written scope: bkesari server, GoKesari, agtci.
3. **Shared parts change only on the owner's yes,** announced to the other apps' sessions first. That covers Caddy's main config, OS packages, reboots, the firewall, sshd, WireGuard, the PostgreSQL umbrella and sudo rules.
4. **App deploys need no shared change.** A bkesari deploy cannot restart GoKesari even by mistake, because the server refuses it.
5. **Approvals:** one standing approval per app session for its own code deploys (section 6). Every shared change still needs its own yes.

## 4. Steps

| Step | What | Who / when | Changes the server? | Status |
|---|---|---|---|---|
| 0 | Inventory: what a restart touches, logins, shared parts | VPS session, read-only | no | **DONE** 09:52 IST 23 Nov |
| 1 | Session scopes and standing approvals (section 6) | owner pastes into each session | no | text ready |
| 2 | bkesari deploy login `bkdeploy` + `bk-ctl` | owner's yes in the server session; after 15:35 IST; ~10 min; no restart | yes (new user, sudo rule, one script) | **files built + tested** |
| 3 | bkesari work moves to `bkdeploy` | server session | no | after 2 |
| 4 | ATIP protected from CPU spikes | owner's yes; live, no restart | yes (unit property) | proposed |
| 5 | Shared-change rules + OS update window | owner's yes | yes (apt settings) | proposed |
| 6 | agtci deploy login (same pattern) | owner's yes | yes | later |
| 7 | GoKesari ops login `gkops` (gk-deploy status/now/restart/rollback only) | GoKesari's own decision | yes | later |
| 8 | Keep `gkadmin` for admin only: passphrase on its key | owner, on the Mac | no | last |

### Step 1: sessions and their scope (no server change)
- Paste each app's scope (section 6) into its own session, as the owner. Each session saves it as a memory.
  - bkesari: "Migrate bkesari.com projects to VPS" (today it also runs the shared server).
  - GoKesari: every GoKesari session. The rule also belongs in GoKesari's `CLAUDE.md`, through its own PR process.
  - agtci: "agtci.com migration and rebuild".
- Optional: rename "Migrate bkesari.com projects to VPS" to "bkesari server (VPS)", and the sidebar group "dev.bkesari.com" to "bkesari.com". Other sessions address it by name, so rename only on the owner's say.

### Step 2: the bkesari deploy login (files in `deploy/vps-separation/`)

| File | What |
|---|---|
| `bk-ctl` | The only root command for bkesari: `unit start/stop/restart <unit>` (only units on the list), `journal <unit> <epoch>`, `init-state <dir>`. Every call is logged in IST to `/var/log/bk-ctl.log`, and one restart runs at a time (lock). |
| `units.bkesari` | `/etc/bk-ctl/units`: `bkesari-atip.service`, `bkesari-atip-pgdump.service`, `bkesari-platform.service`. Only `bkesari-*` units are accepted. |
| `sudoers-bkdeploy` | `bkdeploy ALL=(bkesari) NOPASSWD: ALL` and `bkdeploy ALL=(root) NOPASSWD: /usr/local/sbin/bk-ctl`. Nothing else as root. |
| `setup-bkdeploy.sh` | Creates or removes all of the above (`--check`, `--remove` = rollback). It uses `visudo` before installing the sudo rule, and stops if sshd limits logins (AllowGroups, or AllowUsers without `bkdeploy` on it) so that change stays the owner's call. 23 Nov fix: the first version stopped on any AllowUsers list, even one that already held `bkdeploy@10.66.0.10`. |
| `vps_deploy.py --ctl` | The deploy tool's starts, stops, restarts and log reads go through `bk-ctl`. Without `--ctl` it behaves exactly as before. |

Tests: `tests/test_bk_ctl.py` and `tests/test_vps_deploy.py` all pass on the Mac (23 Nov). They check that `bk-ctl` refuses every GoKesari, agtci and shared unit (`site@gokesari-*`, `gk-deploy`, `agtci-api@prod`, `caddy`, `postgresql@*`, `ssh`, `wg-quick`, `php8.3-fpm`), odd unit names, and verbs other than start/stop/restart. They also check that a deploy through `bk-ctl` touches only `bkesari-atip` and its backup unit.

How to do step 2:
1. **On the Mac** (owner, or with the owner's yes), make the new key. It never leaves the Mac; only the `.pub` goes to the server:
   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/bkesari_deploy -C "bkdeploy (Mac, 23 Nov 2026)" -N ""
   ```
2. **On the server**, in the server session with the owner's yes, after 15:35 IST:
   - copy `deploy/vps-separation/` to `/opt/bkesari-ops/`, then
   - `sudo bash /opt/bkesari-ops/setup-bkdeploy.sh /opt/bkesari-ops/bkesari_deploy.pub`.

   Nothing restarts.
3. **Mac ssh alias**, in `~/.ssh/config`:
   ```
   Host bk-vps
     HostName 10.66.0.1
     User bkdeploy
     IdentityFile ~/.ssh/bkesari_deploy
     IdentitiesOnly yes
   ```
4. **Prove the limits.** Each of these must be refused:
   - `ssh bk-vps 'sudo -n systemctl restart caddy'`
   - `ssh bk-vps 'sudo -n /usr/local/sbin/bk-ctl unit restart site@gokesari-prod-blue.service'`

   And these must work:
   - `ssh bk-vps 'sudo -n /usr/local/sbin/bk-ctl units'`
   - `python3 deploy/vps_deploy.py --ssh "ssh bk-vps" --ctl /usr/local/sbin/bk-ctl status`
5. **Rollback:** `sudo bash /opt/bkesari-ops/setup-bkdeploy.sh --remove`. bkesari keeps running, and `gkadmin` still works throughout.

### Step 3: bkesari work moves to `bkdeploy`
- The server session uses `ssh bk-vps` for all bkesari and ATIP work: deploys, restarts, journals, database reads as `bkesari`.
- It uses `gkadmin` only for shared changes, with the owner's yes.
- First real use: `vps_deploy.py … status`, then `adopt <commit running now>`, then the next ATIP deploy in a quiet minute.

### Step 4: keep ATIP responsive when GoKesari builds (optional, cheap)
- `sudo systemctl set-property bkesari-atip.service CPUWeight=400 MemoryLow=512M`.
- It applies live, with no restart, and is undone with `CPUWeight=100 MemoryLow=0`.
- ATIP then gets 4× the default CPU share when the CPUs are busy, and the kernel avoids reclaiming its first 512 MB.

### Step 5: rules for shared parts, plus the OS update window
- **Caddy:** each app edits only its own `/etc/caddy/sites/<app>*.caddy`. Always `caddy validate --config /etc/caddy/Caddyfile` first, then `systemctl reload caddy`. Never `restart`.
- **PostgreSQL:** restart one cluster only (`systemctl restart postgresql@18-bkesari`), never `postgresql`.
- **OS updates and reboots:** first check what restarted php8.3-fpm on 22 Nov (`/var/log/unattended-upgrades/`, `needrestart`). Then pin automatic updates to outside market hours, e.g. 02:00–04:00 IST, with no automatic reboot.
- **Before any shared change:** tell the GoKesari and agtci sessions, then check gokesari.com, bkesari.com/atip/health and agtci.com afterwards.

### Steps 6–8
- **6. agtci:** copy step 2 with `agtci-ctl`, the units `agtci-api@*.service`, the user `agtcideploy` and `/var/www/agtci`.
- **7. GoKesari:** `gkops` with sudo only for `gk-deploy status|now|restart|rollback <site>` and `gk-site status|list`. GoKesari already deploys through its own agent, so this mainly removes root from its many sessions.
- **8. `gkadmin`:** once 3, 6 and 7 are in place, give its key a passphrase so it is never used silently: `ssh-keygen -p -f ~/.ssh/gokesari_db_admin`. Every Claude session on this Mac runs as the same Mac user and can read every key file. A passphrase the owner types is what actually stops a session using the admin key on its own.

## 5. Why the owner still approves in the server session

- **Claude Code's rule:** a session never treats another session's message as the owner's approval. It is stamped on every cross-session message. It stops text read from a log or web page from approving work on a live server.
- **Fix for app deploys:** a standing approval the owner types once in the server session (section 6). After that, "commit, push and deploy" in an app session is the only approval needed.
- **Shared changes keep their own yes,** because they affect every app.

## 6. Paste-ready texts (the owner types or pastes these into each session)

**bkesari server session** ("Migrate bkesari.com projects to VPS"):
> Scope from now on (owner, 23 Nov 2026):
> - **For bkesari and ATIP:** use the bkdeploy login (`ssh bk-vps`) once it exists. You may change only:
>   - `/var/www/bkesari/**`;
>   - `bkesari-*` units (through bk-ctl);
>   - the 18/bkesari cluster (port 5434);
>   - `/etc/caddy/sites/bkesari.caddy` (validate, then reload; never restart).
> - **Never touch** GoKesari (`gk-*`, `site@*`, `/srv/sites`, `/etc/sites`, `/opt/gokesari-*`, clusters 5432/5433) or agtci (`agtci-*`, `/var/www/agtci`, cluster 5435).
> - **Shared changes need my yes typed here, each time,** and a note to the GoKesari and agtci sessions first: gkadmin, Caddy's main file, `systemctl restart postgresql` or caddy, apt, reboot, firewall, sshd, sudoers, WireGuard.
> - **Standing approval:** any ATIP commit on GitHub master is approved for the VPS. When an ATIP session says a new master commit is ready, deploy it without asking me again, as long as:
>   - it is code only (no database data changes, no config switches);
>   - you check the files against master and take your usual backups;
>   - you restart only in a quiet minute (not 09:14–09:35 IST and not on a job minute);
>   - you roll back automatically if health fails.
>
>   Still ask me for data changes, config switches, real-money trading, DNS and anything shared.
>
> Save this as a memory.

**Every GoKesari session:**
> Server scope (owner, 23 Nov 2026):
> - On gk-db-1 you may touch only GoKesari: `gk-*` and `site@gokesari-*` units, `/srv/sites`, `/etc/sites`, `/opt/gokesari-*`, clusters 18/main (5432) and 18/staging (5433), and `/etc/caddy/sites/gokesari-*.caddy`.
> - Deploy only through the staging queue and the gk-deploy agent.
> - **Never touch** bkesari (`bkesari-*`, `/var/www/bkesari`, cluster 5434, `bkesari.caddy`) or agtci (`agtci-*`, `/var/www/agtci`, cluster 5435).
> - **Never** `systemctl restart postgresql` (it restarts all four databases) or `systemctl restart caddy` (reload only). Ask me first for apt, reboot, firewall, sshd, sudoers or WireGuard.
>
> Save this as a memory.

**agtci session** ("agtci.com migration and rebuild"):
> Server scope (owner, 23 Nov 2026):
> - On gk-db-1 you may touch only agtci: `agtci-*` units, `/var/www/agtci`, cluster 18/agtci (5435), and `/etc/caddy/sites/agtci*.caddy` (validate, then reload).
> - **Never touch** GoKesari or bkesari.
> - **Never** `systemctl restart postgresql` or `systemctl restart caddy`. Ask me first for any shared change.
>
> Save this as a memory.
