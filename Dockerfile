# Image intentionally has no keys: secrets arrive as environment
# variables (GROKBOT_*), the config is mounted as a volume. The built
# image can be stored anywhere — there is nothing in it to trade with.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    GROKBOT_LOG_PATH=/app/logs/trades.jsonl \
    GROKBOT_STATE_PATH=/app/state/pipeline.json

WORKDIR /app

RUN useradd --system --create-home --home-dir /home/grokbot grokbot

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY scripts ./scripts
COPY config.example.yaml pyproject.toml README.md ./

# Logs and state are volumes: they must survive an image rebuild.
RUN mkdir -p /app/logs /app/state /app/config && chown -R grokbot:grokbot /app
VOLUME ["/app/logs", "/app/state"]

USER grokbot

# Liveness comes from the pipeline itself. If health is off (port 0),
# the check always succeeds: then only the restart policy watches the process.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os,sys,urllib.request; \
port=os.environ.get('GROKBOT_HEALTH_PORT','0'); \
sys.exit(0) if port in ('','0') else None; \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz',timeout=3).status==200 else 1)"

# The pipeline handles SIGTERM itself: it finishes in-flight work and saves state.
STOPSIGNAL SIGTERM

ENTRYPOINT ["python", "-m", "src.cli"]
CMD ["run", "--config", "/app/config/config.yaml"]
