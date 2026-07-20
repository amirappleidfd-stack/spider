# Unified Spider Panel + Telegram Bot — Railway Dockerfile
# Multi-stage build for small final image

# ── Build stage ──────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libffi-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Runtime stage ────────────────────────────────────────────────────────────
FROM python:3.11-slim

# Runtime dependencies (minimal)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    libssl3 \
    libffi8 \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY main.py .
COPY bot_integration.py .
COPY pages.py .
COPY static/ static/

# Create data directory for state persistence
RUN mkdir -p /data /app/static/uploads

# Expose default port (overridable via $PORT env var)
EXPOSE 8765

# Health check
HEALTHCHECK --interval=60s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import http.client; c=http.client.HTTPConnection('localhost', int(__import__('os').environ.get('PORT', 8765))); c.request('GET', '/health'); r=c.getresponse(); exit(0 if r.status==200 else 1)"

# Start with uvicorn (main.py handles the run)
CMD ["python", "main.py"]
