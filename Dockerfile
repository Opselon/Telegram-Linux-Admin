# Stage 1: Builder
FROM python:3.12-slim as builder

WORKDIR /app

# Install build dependencies
RUN pip install --upgrade pip build

# Copy project files and build the wheel
COPY . .
RUN python -m build

# Stage 2: Final Image
FROM python:3.12-slim

WORKDIR /app

# Create a non-root user for security. Install the wheel *before* dropping
# privileges: pip run as a normal user installs console scripts into
# ~/.local/bin, which is not on PATH, so the ENTRYPOINT would not resolve.
RUN useradd --create-home appuser

# Copy the built wheel from the builder stage and install it
COPY --from=builder /app/dist/*.whl .
RUN pip install --no-cache-dir *.whl && rm -f ./*.whl \
    && mkdir -p /app/data && chown -R appuser:appuser /app

USER appuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TLA_HEALTH_HOST=0.0.0.0

# Expose the data directory for persistent storage
VOLUME /app/data

# Optional health endpoint (only listens when TLA_HEALTH_PORT is set)
EXPOSE 8081

# Report unhealthy only when the health endpoint is enabled; without
# TLA_HEALTH_PORT the probe is a no-op so the container is never marked down
# for a feature that is deliberately off.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; port = os.environ.get('TLA_HEALTH_PORT'); port and urllib.request.urlopen('http://127.0.0.1:' + port + '/healthz', timeout=4)"

# Set the entrypoint to run the bot
ENTRYPOINT ["tla-bot"]
