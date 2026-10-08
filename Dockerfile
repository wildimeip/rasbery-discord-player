FROM denoland/deno:bin-2.5.6 AS deno

FROM python:3.12-slim-trixie

# Set by the release workflow; shown in the logs at start.
ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    DATA_DIR=/data \
    HOME=/home/player

# mpv plays the audio (ALSA straight to the Pi's sound card); it fetches YouTube streams with
# yt-dlp, and yt-dlp runs YouTube's JavaScript challenges with deno.
RUN apt-get update \
    && apt-get install -y --no-install-recommends mpv ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=deno /deno /usr/local/bin/deno

WORKDIR /app
# Dependencies from pyproject.toml only; the app runs from /app as `python -m player`.
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" > /tmp/requirements.txt \
    && pip install -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt \
    && useradd --uid 1000 --create-home --groups audio player \
    && mkdir -p /data && chown player:player /data
COPY player ./player

# Never runs as root. A bind-mounted /data must be owned by uid 1000 (setup does this).
USER player
VOLUME ["/data"]
# The bot refreshes this file every 30 s while it is connected to Discord and mpv runs.
HEALTHCHECK --interval=60s --timeout=5s --start-period=90s \
  CMD python -c "import os, sys, time; sys.exit(time.time() - os.path.getmtime('/tmp/heartbeat') > 120)"
CMD ["python", "-m", "player"]
