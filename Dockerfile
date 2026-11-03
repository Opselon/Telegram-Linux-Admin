# Stage 1: Web frontend build (React/TS/Vite → static bundle)
FROM node:22-slim AS webbuilder

WORKDIR /web
COPY web/package.json web/package-lock.json* ./
RUN npm install --no-audit --no-fund || npm install --no-audit --no-fund --legacy-peer-deps
COPY web/ ./
RUN npm run build

# Stage 2: Python wheel builder
FROM python:3.12-slim as builder

WORKDIR /app

# Install build dependencies
RUN pip install --upgrade pip build

# Copy project files and build the wheel
COPY . .
RUN python -m build

# Stage 3: Final Image
FROM python:3.12-slim

WORKDIR /app

# Create a non-root user for security. Install the wheel *before* dropping
# privileges: pip run as a normal user installs console scripts into
# ~/.local/bin, which is not on PATH, so the ENTRYPOINT would not resolve.
RUN useradd --create-home appuser

# Copy the built wheel from the builder stage and install it.
# The Bale SDK extra (pure Python) is included so the container runs the
# full platform when BALE_BOT_TOKEN is provided.
COPY --from=builder /app/dist/*.whl .
RUN pip install --no-cache-dir "*.whl[bale]" && rm -f ./*.whl \
    && mkdir -p /app/data /app/web && chown -R appuser:appuser /app

# Static SPA bundle built in stage 1; served by the FastAPI app.
COPY --from=webbuilder /web/dist /app/web/dist

USER appuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TLA_HEALTH_HOST=0.0.0.0 \
    TLA_WEB_DIST=/app/web/dist \
    TLA_WEB_HOST=0.0.0.0 \
    TLA_WEB_PORT=8080

# Expose the data directory for persistent storage
VOLUME /app/data

# Web management API + SPA (bind 0.0.0.0 here; production MUST sit behind a
# TLS reverse proxy — see deploy/README.md) and the legacy health endpoint.
EXPOSE 8080 8081

# Report unhealthy only when the health endpoint is enabled; without
# TLA_HEALTH_PORT the probe checks the web /api/ready endpoint instead.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; port = os.environ.get('TLA_HEALTH_PORT'); port and urllib.request.urlopen('http://127.0.0.1:' + port + '/healthz', timeout=4)" || python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:' + str(__import__('os').environ.get('TLA_WEB_PORT', '8080')) + '/api/ready', timeout=4)"

# Full platform: web API + Bale (when tokened) + Telegram (when tokened)
ENTRYPOINT ["tla-platform"]
