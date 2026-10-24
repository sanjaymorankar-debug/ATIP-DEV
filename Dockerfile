# ATIP container image (W38, OPS-04).
#
#   docker build -t atip:local .
#   docker compose up -d                       # see docker-compose.yml: app + optional PostgreSQL
#
# The image runs the same single process as the laptop install (`python main.py`: scheduler +
# dashboard + index feed). State lives in the /app/atip_data volume (SQLite DB, config.json,
# secrets, logs, backups) -- never in the image. LIVE trading stays off: nothing here changes
# execution.mode, and the image carries no credentials.
#
# BASE_IMAGE (W39b): the same official image from a mirror when Docker Hub rate-limits anonymous
# pulls, e.g. --build-arg BASE_IMAGE=mirror.gcr.io/library/python:3.14-slim (the CI build does this).
ARG BASE_IMAGE=python:3.14-slim
FROM ${BASE_IMAGE}

# The scheduler runs on wall-clock IST (07:00 pre-market, 16:45 post-market ...). UTC would shift
# every job by 5h30.
ENV TZ=Asia/Kolkata \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    ATIP_DASHBOARD_HOST=0.0.0.0

# The current python slim images already ship tzdata; apt runs only on a base that lacks it.
RUN if [ ! -e "/usr/share/zoneinfo/$TZ" ]; then \
        apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*; \
    fi \
 && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
 && useradd --create-home --uid 10001 atip

WORKDIR /app
COPY requirements.txt requirements.lock.txt ./
# The lock is the tested set; requirements.txt adds nothing it lacks but psycopg, installed here:
# the PostgreSQL driver (DBS-05, the server database), harmless when the runtime stays on SQLite.
RUN pip install -r requirements.lock.txt && pip install "psycopg[binary]>=3.2"

COPY --chown=atip:atip . .
RUN mkdir -p /app/atip_data && chown atip:atip /app/atip_data
USER atip
VOLUME ["/app/atip_data"]
EXPOSE 8000

# /health/live: the process answers; /health/ready also checks the database and the scheduler
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=8).status == 200 else 1)"

CMD ["python", "main.py"]
