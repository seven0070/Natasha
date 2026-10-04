# Natasha — production image
#
#   docker build -t natasha .
#   docker run --rm -p 8000:8000 -v natasha-data:/data \
#     -e NATASHA_HOST=0.0.0.0 natasha
#
# The image contains the whole agent: backend, API, web console, migrations and configuration. Local
# model servers (Ollama, LM Studio, vLLM, llama.cpp) are *not* in the image - they run as services and
# the container reaches them at NATASHA_OLLAMA__HOST or the equivalent provider setting; see
# docker-compose.yml for a managed Ollama.
#
# Credentials never enter the image and never enter the environment: provider keys are stored in the
# encrypted vault under $NATASHA_HOME (the volume) and read through the credential broker.
#
# syntax=docker/dockerfile:1

FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NATASHA_HOME=/data \
    NATASHA_HOST=0.0.0.0 \
    NATASHA_PORT=8000 \
    PYTHONPATH=/app/backend:/app

WORKDIR /app

# System packages: tini for signal handling (Python must not be PID 1), and the small set of runtime
# libraries the document/video extras link against. Kept deliberately short.
RUN apt-get update \
 && apt-get install --no-install-recommends -y tini \
 && rm -rf /var/lib/apt/lists/*

# Dependency layer first so a source change does not re-resolve the dependency graph.
COPY pyproject.toml README.md ./
COPY backend ./backend
RUN python -m pip install --upgrade pip \
 && python -m pip install ".[api,crypto,docs]"

# Everything else the runtime reads at startup.
COPY apps ./apps
COPY config ./config
COPY frontend ./frontend
COPY migrations ./migrations
COPY scripts ./scripts
COPY infra ./infra

# A non-root user owns the data volume. Nothing in the image is writable by it except /data.
RUN useradd --create-home --uid 10001 natasha \
 && mkdir -p /data \
 && chown -R natasha:natasha /data \
 && chmod +x /app/infra/docker/entrypoint.sh

USER natasha
VOLUME ["/data"]

# The API refuses unauthenticated requests, so the container is safe to expose once the owner has set
# a passphrase (natasha auth setup). /api/health is intentionally public and is what this checks.
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD python3 -c "import json,os,urllib.request; \
url='http://127.0.0.1:'+os.environ.get('NATASHA_PORT','8000')+'/api/health'; \
runtime=json.load(urllib.request.urlopen(url, timeout=4)).get('runtime', {}); \
raise SystemExit(0 if runtime.get('ok') else 1)"

EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--", "/app/infra/docker/entrypoint.sh"]
