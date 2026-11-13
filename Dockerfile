# ==============================================================================
# Dockerfile: Production Container for 24/7 Hyperliquid Trading Bot & Dashboard
# Optimized for: Render Web Services (750 free hours/month), Railway, Fly.io
# ==============================================================================
FROM python:3.11-slim

# Avoid prompts from apt
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PORT=10000
ENV MAINTENANCE_LOCK=false

# Install minimal OS dependencies for compilation & process supervision
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    procps \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install Python dependencies first for caching
COPY requirements.txt /app/
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir \
        requests \
        eth-account \
        hyperliquid-python-sdk \
        httpx \
        pandas \
        numpy \
        python-dotenv \
        pydantic \
        rich

# Copy repository source code
COPY . /app/

# Ensure state and log directories exist with permissions
RUN mkdir -p /app/state /app/logs /app/historical_data /app/sync_staging && \
    chmod +x /app/scripts/*.sh || true

# Expose Web Dashboard HTTP Port (Render binds dynamically to $PORT)
EXPOSE 10000

# Health check targeting Web Dashboard API state endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://127.0.0.1:${PORT}/api/state || exit 1

# Launch both Dashboard and Trading Bot Supervisor
CMD ["bash", "scripts/start_container.sh"]
