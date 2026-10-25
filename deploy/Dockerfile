# kal in a container. Build from the repo root:
#   docker compose -f deploy/docker-compose.yml up -d --build
# State (config, db, research, kalshi.pem) lives in the /data volume.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    KAL_HOME=/data

# A fixed uid, so files you copy into the volume can be given to this user.
RUN groupadd --system --gid 10001 kal \
 && useradd --system --uid 10001 --gid kal --home-dir /data --shell /usr/sbin/nologin kal \
 && mkdir -p /data /app \
 && chown kal:kal /data \
 && chmod 700 /data

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY kal ./kal

USER kal
VOLUME ["/data"]
EXPOSE 8787

# Is the scheduler still ticking, and is there disk space? (Local only: no network.)
# docker-compose.yml has the same check.
HEALTHCHECK --interval=2m --timeout=30s --start-period=2m --retries=3 \
  CMD ["python", "-m", "kal", "health", "--alive"]

# 0.0.0.0 is needed inside a container. kal refuses to start this way unless
# KAL_DASHBOARD_PASSWORD is set, and docker-compose.yml only opens the port to
# 127.0.0.1 on the server, so reach it with an SSH tunnel.
CMD ["python", "-m", "kal", "app", "--host", "0.0.0.0", "--port", "8787"]
