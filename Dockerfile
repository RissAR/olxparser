# ==============================================================================
# Dockerfile for OLX Kyiv Real Estate Parser & Web UI Dashboard
# Multi-arch lightweight Python slim image optimized for production VDS
# ==============================================================================

FROM python:3.12-slim

LABEL maintainer="OLX Parser Team"
LABEL description="OLX Kyiv Real Estate Scraper & Web UI Dashboard"

# Prevent Python from writing .pyc files and force unbuffered standard I/O
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Kyiv \
    PORT=8000 \
    OLX_DB_PATH=/app/data/olx_storage.db \
    OLX_EXPORTS_DIR=/app/exports

# Install essential system runtime dependencies:
# - tzdata: Europe/Kyiv timezone support for APScheduler cron triggers
# - ca-certificates: TLS root certificates for curl_cffi and HTTPS
# - curl: lightweight health check probing
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install Python dependencies first to leverage Docker layer caching
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt

# Create runtime directories for SQLite persistence and CSV file exports
RUN mkdir -p /app/data /app/exports

# Copy application source files, dictionaries, templates, and static assets
COPY app.py database.py exporter.py normalizer.py scraper.py kyiv_streets.json ./
COPY olx-manual-template.csv olx-commercial-template.csv ./
COPY templates/ ./templates/
COPY static/ ./static/

# Copy and configure entrypoint script
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Expose Web UI and REST API port
EXPOSE 8000

# Container healthcheck against /api/status endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/api/status || exit 1

# Define runtime entrypoint and default start command
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
