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
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY main.py .
COPY bot_integration.py .
COPY pages.py .
COPY static/ static/
COPY requirements.txt .

# Create directories for state and uploads
RUN mkdir -p /app/state /app/static/uploads

# Expose port (Railway provides PORT env var)
EXPOSE 8000

# Non-root user for security
RUN useradd -m -u 1000 spider && chown -R spider:spider /app
USER spider

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import httpx, sys; r=httpx.get('http://localhost:8000/health', timeout=3); sys.exit(0 if r.status_code==200 else 1)"

# Start command
CMD ["python", "main.py"]