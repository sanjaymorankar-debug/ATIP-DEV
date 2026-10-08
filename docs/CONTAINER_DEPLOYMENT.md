# Running ATIP in a container or on a cloud VM (OPS-04)

The laptop install (`python main.py`, `deploy/deploy_release.ps1`) is unchanged. W38 adds a container image and a VM recipe for running the same thing elsewhere.

## Files

| File | What |
|---|---|
| `Dockerfile` | Python 3.14 slim, the tested dependency lock, IST timezone (the scheduler runs on wall-clock IST), non-root user, `/app/atip_data` volume, health check on `/health/live`. |
| `.dockerignore` | Keeps `atip_data/`, `.env`, databases, logs, git history and local tooling state (`.claude/`, editor folders) out of the image. |
| `.github/workflows/docker.yml` | W39b: builds the image on every PR and push, runs the `--init` first start, waits for `/health/ready`, and checks the image (below). Nothing is pushed to a registry. |
| `docker-compose.yml` | The `atip` service plus an optional `postgres` profile. **Every port is published on the host's 127.0.0.1 only.** |
| `deploy/atip.env.example` | The environment template: secrets, encryption key, optional `ATIP_DATABASE_URL`. Copy it to `deploy/atip.env`, which git ignores. |
| `deploy/cloud/cloud-init.yaml` | A provider-neutral Ubuntu VM setup. The firewall admits SSH only; it installs Docker, clones a pinned tag and adds a systemd unit. |

## Local Docker

```
cp deploy/atip.env.example deploy/atip.env        # fill it in
docker compose up -d --build
docker compose logs -f atip
```

**First start:**
1. Copy an existing `atip_data` into the volume, or let `--init` create a fresh database:
   `docker compose run --rm atip python main.py --init`
2. Open http://127.0.0.1:8000.

## Cloud VM

1. Create an Ubuntu 24.04 VM (2 vCPU, 4 GB, 40 GB). Paste `deploy/cloud/cloud-init.yaml` as user data, after setting your SSH key and the release tag.
2. Copy the secrets over SSH: `deploy/atip.env` and `atip_data/config.json`. **Never put secrets in user data.**
3. Run `sudo systemctl restart atip`.
4. Reach the dashboard through a tunnel: `ssh -L 8000:127.0.0.1:8000 atip@<vm>`.

## What is deliberately not here

- **Public exposure** (TLS, domain, ports 80/443). That is ENT-07 and needs its own review. Rebinding to 0.0.0.0 "to make it work" leaves an API without sign-in on the internet. The compliance check `dashboard_exposure` fails exactly that case.
- **Live trading.** The image changes nothing about the master switch.

## Base image and Docker Hub limits

`BASE_IMAGE` defaults to `python:3.14-slim`. When Docker Hub refuses anonymous pulls (429), build from a mirror of the same official image:

```
docker build --build-arg BASE_IMAGE=mirror.gcr.io/library/python:3.14-slim -t atip:local .
```

The current slim images already ship tzdata, so the build only runs `apt-get` on a base that lacks it.

## Status

**Built and verified (W39b, 2026-10-08).** The image built from this repository and passed every check below, on a dev container and in the `docker` CI job:

| Check | Result |
|---|---|
| `docker compose config` | valid (with `deploy/atip.env` from the example) |
| `docker build` | 20 MB of application files on the slim base; no `.git`, `.github`, `.claude` or `.env` |
| A container on an empty volume | `/health/live` 200; `/health/ready` reports `database: DEGRADED` until `--init`. This is expected: it is the documented first-start step. |
| `python main.py --init`, then start | `/health/live` 200 and `/health/ready` 200 (`READY`) |
| User, timezone | uid 10001 (`atip`), `+0530` (IST) |
| State and secrets | `/app/atip_data` is empty in the image |
| Live trading | `execution.config.live_gate()` is closed: "execution.mode is PAPER" |

The only errors in the log are the expected "Dhan credentials not set": the image carries no credentials.

**Not verified:** a Kubernetes manifest, or a published registry image. Both are owner decisions (ENT-07 exposure, ENT-16 host).
