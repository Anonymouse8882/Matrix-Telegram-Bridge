# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

# matrix-nio (E2EE extras aside) needs libolm only if you enable encryption.
# The base install here targets unencrypted rooms; see README for E2EE notes.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code.
COPY bridge ./bridge

# Run as a non-root user; persist the Matrix store on a volume.
RUN useradd --create-home --uid 10001 bridge \
    && mkdir -p /data/store \
    && chown -R bridge:bridge /app /data
USER bridge

VOLUME ["/data"]
ENV BRIDGE_CONFIG=/config/config.yaml \
    LOG_LEVEL=INFO

ENTRYPOINT ["python", "-m", "bridge"]
