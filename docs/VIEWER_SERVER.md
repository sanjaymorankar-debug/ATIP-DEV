# ATIP on a web server, view only

One codebase, two copies:

| | **Mac (TRADER)** | **Web server (VIEWER)** |
|---|---|---|
| Code | `master` | the same `master` (same release) |
| Runs | everything: schedule, Dhan feeds, order monitor, AUTO sessions, Telegram | the dashboard only |
| Data | fetches it | a copy pushed from the Mac every 10 minutes |
| Keys | Dhan, Telegram, Anthropic | **none** |
| Orders | yes (PAPER until you decide otherwise) | **none, not even paper** |
| Switch | nothing to set | `ATIP_ROLE=viewer` |

The Mac sends a finished copy outward. Nothing travels back: the web server holds no route, key or
credential that reaches the Mac.

## What the viewer refuses

`ATIP_ROLE=viewer` (or `"role": "viewer"` in that server's `atip_data/config.json`) is read by
[ops/role.py](../ops/role.py). Any value other than `trader` also means VIEWER, so a typo can only stop a
copy from trading, never start it.

| On a VIEWER | Where |
|---|---|
| `main.py` starts the dashboard and the sync watcher, nothing else: no scheduler, no Dhan feeds | `main.py` |
| `--scheduler`, `--run`, `--live-feed`, `--dhan-*`, `--test-telegram`, `--morning-brief`, `--purge-db`, `--login-zerodha` exit with an error | `ops/role.blocked_commands` |
| The order-rules monitor never starts | `dashboard/server.py` |
| Every POST / PUT / PATCH / DELETE gets **403** "view only", except sign-in | `ops/role.install_guard` |
| No Dhan client, for data or orders | `data/dhan.get_dhan_client` |
| No execution client, not even PAPER; no Dhan sandbox client | `orders/environment.py` |
| The paper broker places nothing | `orders/paper.PaperBroker.place_order` |
| No broker connector (Dhan, Zerodha, Upstox, Angel) | `brokers/base.BrokerConnector` |
| No Telegram message (alerts are still listed on the dashboard) | `alerts/telegram.notify` |
| The Dhan token is never renewed here, so the Mac's token can't be cut off | `tools/dhan_token_refresh.py` |
| Every page shows "👁 View only: trading, orders and changes run on the Mac", with the time of the Mac's copy | `dashboard/global_asia.py` |

## How the data gets there

[ops/viewer_sync.py](../ops/viewer_sync.py):

1. **Mac, every 10 minutes** (`com.atip.viewer-push`): SQLite's backup API makes a consistent copy of
   `atip.db` without stopping ATIP. The sign-in credential tables (`enterprise_user`, sessions, API keys,
   tokens) are emptied in the copy. A `manifest.json` records its sha256. rsync over SSH sends the database
   first and the manifest second, and only the pages that changed travel.
2. **Server, every 60 seconds**: when a new manifest arrives, the watcher copies the database, checks it
   against the sha256 and installs it with the same backup API, in one transaction. A page being served sees
   the old copy or the new one, never half of each.

While ATIP is stopped (21:00 to 06:00 IST) the Mac pushes at most once an hour. The first push sends the whole
database, 466 MB on 30 Oct 2026, compressed in transit. After that rsync sends only the changed blocks. A quiet
five minutes changed 43 KB that day; market hours change more. Making the copy took 1.3 s on the Mac, and
installing it on the viewer took about 3 s.

## Set up the server

On a VPS, never shared hosting ([HOSTED_DEPLOYMENT.md §1](HOSTED_DEPLOYMENT.md)). Size: 2 vCPU, 4 GB, 40 GB.
[deploy/cloud/cloud-init.yaml](../deploy/cloud/cloud-init.yaml) prepares one, rsync included.

```bash
cd /home/atip/atip && git pull --ff-only     # the same release as the Mac
mkdir -p viewer_incoming
cp deploy/atip.env.example deploy/atip.env   # leave every key EMPTY on this server
```

In `deploy/atip.env`, add `ATIP_ROOT_PATH=/ATIP` if the viewer is served under `/ATIP/`. Then create the empty
schema once, so the first start does not log `migration 0001 ... baseline tables missing` before the Mac's first
copy arrives, and start it:

```bash
docker compose -f docker-compose.yml -f docker-compose.viewer.yml build
docker compose -f docker-compose.yml -f docker-compose.viewer.yml run --rm atip python main.py --init
docker compose -f docker-compose.yml -f docker-compose.viewer.yml up -d
docker compose logs atip | grep -i viewer    # "Starting ATIP as a VIEWER", "Viewer sync watching ..."
```

`docker-compose.viewer.yml` sets `ATIP_ROLE=viewer` and mounts `viewer_incoming/` read-only. It also sets
`ATIP_ENV=staging`, because `production` requires ATIP's own sign-in (`enterprise.enabled`), and those
accounts stay on the Mac.

**Sign-in is the proxy's job, and it is not optional.** Put nginx with TLS and HTTP Basic auth in front of
every route, exactly as in [HOSTED_DEPLOYMENT.md §2 and §4.1](HOSTED_DEPLOYMENT.md). The viewer can't trade,
but its pages still show your portfolio, positions and P&L. Keep the firewall at 22, 80 and 443 only.

## Set up the Mac

```bash
ssh-keygen -t ed25519 -f ~/.ssh/atip_viewer -N "" -C "atip viewer push"
# append ~/.ssh/atip_viewer.pub to /home/atip/.ssh/authorized_keys on the server, then accept its host key once:
ssh -i ~/.ssh/atip_viewer atip@<server> true
```

`atip_data/viewer_push.json` (git-ignored):

```json
{
  "target":   "atip@<server>:/home/atip/atip/viewer_incoming/",
  "ssh_key":  "~/.ssh/atip_viewer",
  "ssh_port": 22
}
```

```bash
.venv/bin/python -m ops.viewer_sync push --dry-run   # makes the copy, sends nothing
./push_viewer.sh && tail atip_data/viewer_push.log   # first real push
deploy/launchd/install.sh viewer                     # every 10 minutes from now on
```

The copy lives in `atip_data/viewer_push/` and takes as much disk as the database.

## Check it

```bash
# on the server
curl -s http://127.0.0.1:8000/api/status | python3 -m json.tool | grep -A5 '"viewer"'
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8000/api/orders     # 403
docker compose exec atip python -m ops.viewer_sync status

# on the Mac
launchctl list | grep com.atip.viewer-push
.venv/bin/python -m ops.viewer_sync status
```

## Things to know

- **Same release on both.** Deploy the Mac and the server together. A server that is newer than the Mac adds
  its missing tables to each copy (its additive migrations run again after every install). A server that is
  older than the Mac may not know the Mac's newest tables.
- **Prices on the viewer are the Mac's.** The viewer has no Dhan connection, so its live-quote poll returns
  nothing and pages show the last prices in the copy, up to 10 minutes old.
- **Turning the viewer off** is `docker compose down` on the server and
  `launchctl bootout gui/$UID/com.atip.viewer-push` on the Mac. Nothing on the Mac depends on it.
- **The read-only snapshot still works.** [publish_snapshot.py](../publish_snapshot.py) pushes one rendered
  page with no server to run. It is the lighter option when one page is enough.
