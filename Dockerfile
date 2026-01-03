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

# Create a non-root user for security
RUN useradd --create-home appuser
USER appuser

# Copy the built wheel from the builder stage and install it
COPY --from=builder /app/dist/*.whl .
RUN pip install --no-cache-dir *.whl

# Expose the data directory for persistent storage
VOLUME /app/data

# Set the entrypoint to run the bot
ENTRYPOINT ["tla-bot"]
